// Command e2e-probe tests the whole path without a browser. It plays the role of
// a user: it joins the Stage with your participant token using the IVS
// Linux broadcast SDK, waits for the bot's audio and for the bot to subscribe,
// speaks a WAV file into the Stage, and records what the bot says back.
//
// It exits 0 if the bot answered with at least -min-reply of audible speech.
// Transcripts and latency are in the bridge's log.
package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"sync"
	"time"

	ivsrealtime "github.com/aws/amazon-ivs-realtime-go"

	"ivsnovasonic/bridge/internal/audio"
)

// voicedRMS separates speech from the silence the bot publishes between turns.
const voicedRMS = 300

var (
	token    = flag.String("token", os.Getenv("IVS_PARTICIPANT_TOKEN"), "the probe's (user's) participant token for the bot's Stage (default $IVS_PARTICIPANT_TOKEN); needs PUBLISH and SUBSCRIBE")
	wavPath  = flag.String("wav", "question.wav", "16-bit PCM WAV to speak into the Stage")
	outPath  = flag.String("out", "reply.wav", "where to write the bot's recorded reply")
	timeout  = flag.Duration("timeout", 30*time.Second, "how long to wait for a reply after speaking")
	minReply = flag.Duration("min-reply", time.Second, "minimum audible reply for success")
	settle   = flag.Duration("settle", 2*time.Second, "how long to wait after hearing the bot before speaking, so the bot can subscribe")
)

func main() {
	flag.Parse()
	log.SetFlags(log.Ltime | log.Lmicroseconds)
	ok, err := run()
	if err != nil {
		log.Fatalf("FAIL: %v", err)
	}
	if !ok {
		os.Exit(1)
	}
}

func run() (bool, error) {
	if *token == "" {
		return false, fmt.Errorf("pass the user token with -token or IVS_PARTICIPANT_TOKEN")
	}
	wav, err := audio.ReadWAV(*wavPath)
	if err != nil {
		return false, err
	}

	source, err := ivsrealtime.NewAudioSource(uint32(wav.SampleRate), 1, ivsrealtime.PCMFormatS16Interleaved)
	if err != nil {
		return false, err
	}
	defer source.Close()
	playout := audio.NewPlayout(source, wav.SampleRate)

	rec := &recorder{}
	handler := &probeEvents{rec: rec, subscribed: make(chan struct{})}
	strategy := &probeStrategy{source: source, joined: make(chan string, 1)}
	stage, err := ivsrealtime.NewStage(*token, strategy, handler)
	if err != nil {
		return false, err
	}
	defer stage.Close()
	if err := stage.Join(); err != nil {
		return false, err
	}

	// The SDK asks the strategy whether to publish once the probe has joined,
	// which is the first point its own participant ID is known.
	var participantID string
	select {
	case participantID = <-strategy.joined:
	case <-time.After(15 * time.Second):
		return false, fmt.Errorf("never joined the Stage as a publisher; check the token is valid, unused, and has PUBLISH and SUBSCRIBE")
	}
	log.Printf("joined as %s; waiting for the bot (bridge log: conversation started user=%s)", participantID, participantID)

	select {
	case <-handler.subscribed:
	case <-time.After(15 * time.Second):
		return false, fmt.Errorf("never received audio from a role=bot participant; is the bridge running, and is its bot token for this Stage with --attributes role=bot?")
	}
	// The bot subscribes to the probe once it sees it, which takes a moment.
	// There is no signal for that here, so wait a little.
	time.Sleep(*settle)

	log.Printf("speaking %s (%.1f s at %d Hz)", *wavPath, float64(len(wav.Samples))/float64(wav.SampleRate), wav.SampleRate)
	playout.Start()
	defer playout.Stop()
	playout.Write(audio.S16ToBytes(nil, wav.Samples))
	for playout.Stats().Buffered > 0 {
		time.Sleep(10 * time.Millisecond)
	}
	spokeAt := time.Now()
	rec.markSpeechEnd(spokeAt)
	log.Printf("finished speaking; waiting up to %s for a reply", *timeout)

	deadline := time.After(*timeout)
	for done := false; !done; {
		select {
		case <-deadline:
			done = true
		case <-time.After(100 * time.Millisecond):
			done = rec.replyComplete(*minReply)
		}
	}

	r := rec.result()
	if r.rate > 0 {
		if err := audio.WriteWAV(*outPath, r.rate, r.samples); err != nil {
			return false, err
		}
		log.Printf("wrote %s (%.1f s at %d Hz)", *outPath, float64(len(r.samples))/float64(r.rate), r.rate)
	}
	if r.firstVoice.IsZero() {
		log.Printf("FAIL: no audible reply within %s. Check the bridge log: is the bot talking with another user, or did it start a conversation with %s?", *timeout, participantID)
		return false, nil
	}
	log.Printf("time to first audio: %d ms (from end of question to first audible reply at the probe)",
		r.firstVoice.Sub(spokeAt).Milliseconds())
	log.Printf("audible reply: %.1f s", r.voiced.Seconds())
	if r.voiced < *minReply {
		log.Printf("FAIL: reply shorter than %s", *minReply)
		return false, nil
	}
	log.Printf("PASS (transcripts and time to first audio at the bridge are in the bridge log)")
	return true, nil
}

// probeStrategy subscribes to the bot, publishes the question, and reports the
// probe's own participant ID.
type probeStrategy struct {
	source *ivsrealtime.AudioSource
	joined chan string
	once   sync.Once
}

func (*probeStrategy) SubscriptionTypes(p ivsrealtime.ParticipantInfo) ivsrealtime.SubscribeType {
	if p.Attributes["role"] == "bot" {
		return ivsrealtime.SubscribeTypeAudio
	}
	return ivsrealtime.SubscribeTypeNone
}

func (s *probeStrategy) ShouldPublish(p ivsrealtime.ParticipantInfo) bool {
	s.once.Do(func() { s.joined <- p.ParticipantID })
	return true
}

func (s *probeStrategy) StreamsToPublish(ivsrealtime.ParticipantInfo) ivsrealtime.PublishStreams {
	return ivsrealtime.PublishStreams{Audio: s.source}
}

type probeEvents struct {
	ivsrealtime.BaseStageEventCallbacks
	rec        *recorder
	subscribed chan struct{}
	once       sync.Once
}

func (e *probeEvents) OnConnectionStateChanged(_ *ivsrealtime.Stage, state ivsrealtime.ConnectionState, err error) {
	log.Printf("probe connection: %s %v", state, errOrEmpty(err))
}

func (e *probeEvents) OnStreamsAdded(_ *ivsrealtime.Stage, p ivsrealtime.ParticipantInfo, streams []ivsrealtime.StageStream) {
	if p.Attributes["role"] != "bot" {
		return
	}
	for _, st := range streams {
		if a, ok := st.(ivsrealtime.AudioStageStream); ok {
			if err := a.SetPCMCallback(e.rec.onPCM); err != nil {
				log.Printf("subscribe to bot audio: %v", err)
				continue
			}
			e.once.Do(func() { close(e.subscribed) })
		}
	}
}

func errOrEmpty(err error) any {
	if err == nil {
		return ""
	}
	return err
}

// recorder keeps the bot's audio and measures the reply.
type recorder struct {
	mu         sync.Mutex
	rate       int
	samples    []int16
	speechEnd  time.Time
	firstVoice time.Time
	lastVoice  time.Time
	voiced     time.Duration
	mono       []float32
}

// onPCM runs on the SDK's audio thread; the work is a few microseconds per
// 10 ms buffer, well within budget.
func (r *recorder) onPCM(buf ivsrealtime.PCMBuffer) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.rate == 0 {
		r.rate = int(buf.SamplesPerSec)
	}
	if int(buf.SamplesPerSec) != r.rate {
		return
	}
	var err error
	r.mono, err = audio.DecodeMono(r.mono[:0], buf.Data, buf.BytesPerSample, buf.NumChannels)
	if err != nil {
		return
	}
	start := len(r.samples)
	r.samples = audio.FloatToS16(r.samples, r.mono)
	if r.speechEnd.IsZero() || audio.RMS(r.samples[start:]) < voicedRMS {
		return
	}
	now := time.Now()
	if r.firstVoice.IsZero() {
		r.firstVoice = now
	}
	r.lastVoice = now
	r.voiced += time.Duration(len(r.samples)-start) * time.Second / time.Duration(r.rate)
}

func (r *recorder) markSpeechEnd(t time.Time) {
	r.mu.Lock()
	r.speechEnd = t
	r.mu.Unlock()
}

// replyComplete reports whether the bot has said enough and then gone quiet.
func (r *recorder) replyComplete(minVoiced time.Duration) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.voiced >= minVoiced && time.Since(r.lastVoice) > 1500*time.Millisecond
}

type recording struct {
	rate       int
	samples    []int16
	firstVoice time.Time
	voiced     time.Duration
}

func (r *recorder) result() recording {
	r.mu.Lock()
	defer r.mu.Unlock()
	return recording{rate: r.rate, samples: append([]int16(nil), r.samples...), firstVoice: r.firstVoice, voiced: r.voiced}
}
