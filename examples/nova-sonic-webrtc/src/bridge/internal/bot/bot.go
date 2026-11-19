package bot

import (
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"

	ivsrealtime "github.com/aws/amazon-ivs-realtime-go"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"

	"ivsnovasonic/bridge/internal/audio"
	"ivsnovasonic/bridge/internal/nova"
)

const (
	// uplinkFrame is 32 ms at 24 kHz, the cadence Nova Sonic expects.
	uplinkFrame = 768
	// idleBeforeSilence is how long the user may deliver no audio before the
	// bridge starts sending silence to keep the Nova Sonic session alive.
	idleBeforeSilence = 200 * time.Millisecond
	statsInterval     = 10 * time.Second
	// joinTimeout bounds how long startup waits for the bot to be publishing.
	joinTimeout = 15 * time.Second
)

// Config configures the bot.
type Config struct {
	BotToken     string                 // participant token the bot joins with; never logged
	Bedrock      *bedrockruntime.Client // opens a Nova Sonic stream per conversation
	ModelID      string
	SystemPrompt string
	VoiceID      string
	SessionLimit time.Duration // ends each Nova Sonic session before Bedrock's 8-minute stream limit
}

// Bot is the bridge's single, long-lived Stage participant.
type Bot struct {
	cfg Config
	log *slog.Logger

	stage   *ivsrealtime.Stage
	source  *ivsrealtime.AudioSource
	playout *audio.Playout
	strat   *strategy

	// pcm carries user audio from the SDK's real-time audio thread to the uplink
	// goroutine. The audio thread never blocks on it; overflow is dropped.
	pcm       chan audio.InputFrame
	droppedIn atomic.Int64

	// Cumulative counters, logged in audio stats, that show where audio stops.
	userBuffers  atomic.Int64 // buffers received from the user's stream
	framesToNova atomic.Int64 // 32 ms frames sent to Nova Sonic, including silence
	novaAudio    atomic.Int64 // bytes of assistant audio received from Nova Sonic

	// nova is the current conversation's Nova Sonic session, or nil between
	// conversations. The uplink sends to whatever it holds.
	nova atomic.Pointer[nova.Session]

	ctrl   chan ctrlMsg
	joined chan ivsrealtime.ParticipantInfo
	fatal  chan error

	stop      chan struct{}
	wg        sync.WaitGroup
	closeOnce sync.Once
}

// Start joins the Stage with cfg.BotToken and waits until the bot is
// publishing. It fails if the token is rejected, or if the bot never starts
// publishing (for example, the token lacks PUBLISH or was already used).
func Start(cfg Config, log *slog.Logger) (*Bot, error) {
	b := &Bot{
		cfg:    cfg,
		log:    log,
		pcm:    make(chan audio.InputFrame, 256), // about 2.5 s of 10 ms buffers
		ctrl:   make(chan ctrlMsg, 256),
		joined: make(chan ivsrealtime.ParticipantInfo, 1),
		fatal:  make(chan error, 1),
		stop:   make(chan struct{}),
	}
	var err error
	b.source, err = ivsrealtime.NewAudioSource(nova.SampleRate, 1, ivsrealtime.PCMFormatS16Interleaved)
	if err != nil {
		return nil, fmt.Errorf("bot: create audio source: %w", err)
	}
	b.playout = audio.NewPlayout(b.source, nova.SampleRate)
	b.strat = &strategy{
		source: b.source,
		onJoined: func(p ivsrealtime.ParticipantInfo) {
			select {
			case b.joined <- p:
			default:
			}
		},
		onSeen: func(p ivsrealtime.ParticipantInfo) {
			if isUser(p) {
				b.post(ctrlMsg{kind: msgSeen, participantID: p.ParticipantID})
			}
		},
	}

	b.stage, err = ivsrealtime.NewStage(cfg.BotToken, b.strat, &stageEvents{b: b})
	if err != nil {
		b.source.Close()
		return nil, fmt.Errorf("bot: %w", describeTokenError(err))
	}
	fail := func(err error) (*Bot, error) {
		b.stage.Close()
		b.source.Close()
		return nil, err
	}
	if err := b.stage.Join(); err != nil {
		return fail(fmt.Errorf("bot: join stage: %w", describeTokenError(err)))
	}

	select {
	case p := <-b.joined:
		log.Info("bot joined the Stage", "bot", p.ParticipantID)
	case err := <-b.fatal:
		return fail(fmt.Errorf("bot: joining the Stage failed: %w", describeTokenError(err)))
	case <-time.After(joinTimeout):
		return fail(errors.New("bot: joined but never started publishing; check BOT_TOKEN is unexpired, unused, and has PUBLISH and SUBSCRIBE"))
	}

	b.playout.Start()
	b.wg.Add(2)
	go b.runUplink()
	go b.runController()
	return b, nil
}

// Fatal reports an error the bot cannot recover from, such as losing its Stage
// connection. The bridge should exit; restart it with a fresh BOT_TOKEN.
func (b *Bot) Fatal() <-chan error { return b.fatal }

// Close ends any conversation, leaves the Stage, and releases everything. The
// order matters: nothing may call into the Stage or push audio once they are
// closed, and the Stage must close before the source it publishes.
func (b *Bot) Close() {
	b.closeOnce.Do(func() {
		close(b.stop)
		b.wg.Wait()
		b.playout.Stop()
		b.stage.Close()
		b.source.Close()
		b.logStats()
	})
}

// post hands a message to the controller from an SDK callback, which must not
// block.
func (b *Bot) post(m ctrlMsg) {
	select {
	case b.ctrl <- m:
	default:
		b.log.Warn("dropping stage event; controller is backed up", "kind", m.kind)
	}
}

// onUserPCM runs on the SDK's real-time audio thread. It must not block.
func (b *Bot) onUserPCM(buf ivsrealtime.PCMBuffer) {
	b.userBuffers.Add(1)
	select {
	case b.pcm <- audio.InputFrame{
		Data:           buf.Data,
		BytesPerSample: buf.BytesPerSample,
		NumChannels:    buf.NumChannels,
		SampleRate:     int(buf.SamplesPerSec),
	}:
	default:
		b.droppedIn.Add(1)
	}
}

func (b *Bot) runUplink() {
	defer b.wg.Done()
	up := audio.NewUplink(nova.SampleRate, uplinkFrame, func(frame []byte) {
		if s := b.nova.Load(); s != nil {
			s.SendAudio(frame)
			b.framesToNova.Add(1)
		}
	})
	tick := time.NewTicker(uplinkFrame * time.Second / nova.SampleRate)
	defer tick.Stop()
	stats := time.NewTicker(statsInterval)
	defer stats.Stop()

	lastInput := time.Now()
	loggedFormat := false
	rateChanges := 0
	for {
		select {
		case <-b.stop:
			return
		case f := <-b.pcm:
			if !loggedFormat {
				b.log.Info("user audio format", "sampleRate", f.SampleRate, "channels", f.NumChannels,
					"bytesPerSample", f.BytesPerSample, "framesPerBuffer", len(f.Data)/max(1, f.BytesPerSample*f.NumChannels))
				loggedFormat = true
			}
			if err := up.Process(f); err != nil {
				b.log.Warn("dropping user audio buffer", "err", err)
			}
			if up.RateChanges != rateChanges {
				rateChanges = up.RateChanges
				b.log.Warn("user audio sample rate changed", "sampleRate", f.SampleRate)
			}
			lastInput = time.Now()
		case <-tick.C:
			if b.nova.Load() != nil && time.Since(lastInput) > idleBeforeSilence {
				up.Silence()
			}
		case <-stats.C:
			if b.nova.Load() != nil {
				b.logStats()
			}
		}
	}
}

func (b *Bot) logStats() {
	ps := b.playout.Stats()
	var novaDropped int64
	if s := b.nova.Load(); s != nil {
		novaDropped = s.DroppedAudio.Load()
	}
	b.log.Info("audio stats",
		"userBuffersReceived", b.userBuffers.Load(),
		"framesSentToNova", b.framesToNova.Load(),
		"novaAudioReceivedMs", b.novaAudio.Load()/2*1000/nova.SampleRate,
		"userBuffersDropped", b.droppedIn.Load(),
		"novaFramesDropped", novaDropped,
		"playoutBufferedMs", ps.Buffered.Milliseconds(),
		"playoutPushedSec", ps.PushedFrames/nova.SampleRate,
		"playoutSilentSec", ps.SilentFrames/nova.SampleRate,
		"playoutFlushedMs", ps.FlushedSamples*1000/nova.SampleRate,
		"playoutPushErrors", ps.PushErrors)
}

// stageEvents handles Stage callbacks. They all arrive on one serialized SDK
// thread, which must never block, so anything slow goes to the controller.
type stageEvents struct {
	ivsrealtime.BaseStageEventCallbacks
	b *Bot
}

func (e *stageEvents) OnConnectionStateChanged(_ *ivsrealtime.Stage, state ivsrealtime.ConnectionState, err error) {
	e.b.log.Info("bot connection state", "state", state, "err", err)
	if isFatal(err) {
		e.b.reportFatal(err)
	}
}

func (e *stageEvents) OnParticipantJoined(_ *ivsrealtime.Stage, p ivsrealtime.ParticipantInfo) {
	if isUser(p) {
		e.b.post(ctrlMsg{kind: msgSeen, participantID: p.ParticipantID})
	}
}

// OnParticipantLeft also fires when a participant stops publishing; either way
// the bot can no longer hear them.
func (e *stageEvents) OnParticipantLeft(_ *ivsrealtime.Stage, p ivsrealtime.ParticipantInfo) {
	if !p.IsLocal {
		e.b.post(ctrlMsg{kind: msgLeft, participantID: p.ParticipantID})
	}
}

func (e *stageEvents) OnParticipantSubscribeStateChanged(_ *ivsrealtime.Stage, p ivsrealtime.ParticipantInfo, state ivsrealtime.SubscribeState) {
	e.b.log.Info("subscribe state", "user", p.ParticipantID, "state", state, "active", e.b.strat.isActive(p.ParticipantID))
}

func (e *stageEvents) OnStreamsMutedChanged(_ *ivsrealtime.Stage, p ivsrealtime.ParticipantInfo, streams []ivsrealtime.StageStream) {
	for _, st := range streams {
		if a, ok := st.(ivsrealtime.AudioStageStream); ok && !p.IsLocal {
			e.b.log.Info("user audio mute changed", "user", p.ParticipantID, "muted", a.IsMuted())
		}
	}
}

func (e *stageEvents) OnStreamsRemoved(_ *ivsrealtime.Stage, p ivsrealtime.ParticipantInfo, streams []ivsrealtime.StageStream) {
	if !p.IsLocal {
		e.b.log.Info("streams removed", "user", p.ParticipantID, "count", len(streams))
	}
}

// OnStreamsAdded attaches to the active user's audio. The bot subscribes to no
// one else, so other participants' streams do not arrive here.
func (e *stageEvents) OnStreamsAdded(_ *ivsrealtime.Stage, p ivsrealtime.ParticipantInfo, streams []ivsrealtime.StageStream) {
	if p.IsLocal || !e.b.strat.isActive(p.ParticipantID) {
		return
	}
	for _, st := range streams {
		a, ok := st.(ivsrealtime.AudioStageStream)
		if !ok {
			continue
		}
		if err := a.SetPCMCallback(e.b.onUserPCM); err != nil {
			e.b.log.Error("subscribe to user audio failed", "user", p.ParticipantID, "err", err)
			continue
		}
		e.b.log.Info("receiving user audio", "user", p.ParticipantID, "stream", a.StreamID(), "muted", a.IsMuted())
	}
}

func (e *stageEvents) OnError(_ *ivsrealtime.Stage, err error) {
	e.b.log.Warn("stage error", "err", err)
	if isFatal(err) {
		e.b.reportFatal(err)
	}
}

// isUser reports whether p is someone the bot should talk with. The attribute
// role=bot marks another bot, which never is.
func isUser(p ivsrealtime.ParticipantInfo) bool {
	return !p.IsLocal && p.Attributes["role"] != "bot"
}

func (b *Bot) reportFatal(err error) {
	select {
	case b.fatal <- err:
	default:
	}
}

func isFatal(err error) bool {
	var ivsErr *ivsrealtime.Error
	return errors.As(err, &ivsErr) && ivsErr.IsFatal
}

// describeTokenError adds a hint when the SDK rejected the bot token.
func describeTokenError(err error) error {
	switch {
	case errors.Is(err, &ivsrealtime.Error{Code: ivsrealtime.ErrorCodeStageExpiredToken}):
		return fmt.Errorf("BOT_TOKEN has expired; create a new one: %w", err)
	case errors.Is(err, &ivsrealtime.Error{Code: ivsrealtime.ErrorCodeStageInvalidToken}):
		return fmt.Errorf("BOT_TOKEN is not a valid participant token: %w", err)
	}
	return err
}
