package bot

import (
	"log/slog"
	"slices"
	"time"

	"ivsnovasonic/bridge/internal/nova"
)

type msgKind string

const (
	msgSeen      msgKind = "seen"       // a remote participant is on the Stage
	msgLeft      msgKind = "left"       // a remote participant left or stopped publishing
	msgNovaEnded msgKind = "nova_ended" // Nova Sonic's session ended on its own
)

type ctrlMsg struct {
	kind          msgKind
	participantID string
	conv          *conversation
	reason        string
}

const (
	// A conversation that ends sooner than this counts as a failure, so the
	// next attempt waits. This keeps a failing agent from being retried in a
	// tight loop.
	healthyConversation = 15 * time.Second
	minRetryDelay       = 5 * time.Second
	maxRetryDelay       = time.Minute
)

// conversation is one user's session with Nova Sonic.
type conversation struct {
	b         *Bot
	userID    string
	session   *nova.Session
	log       *slog.Logger
	startedAt time.Time

	// Touched only by the Nova Sonic session's reader goroutine.
	// userTurnAt is set when the user's speech is transcribed and cleared by the
	// first assistant audio after it, for turn latency.
	userTurnAt time.Time
	gotAudio   bool
}

// controller decides who the bot talks to. It runs on its own goroutine and is
// the only caller of Stage methods after startup.
//
// Users are served in the order the bot first sees them. When a Nova Sonic
// session ends on its own (for example at the session limit), the user goes to the back of
// the line and a fresh Nova Sonic session starts when it is their turn again.
// A conversation ends for good only when the user leaves the Stage.
type controller struct {
	b       *Bot
	active  *conversation
	waiting []string
	known   map[string]bool // on the Stage: waiting or active

	// Backoff after failures: no conversation starts before retryAt, and
	// retryC fires then to resume.
	retryDelay time.Duration
	retryAt    time.Time
	retryC     <-chan time.Time
}

func (b *Bot) runController() {
	defer b.wg.Done()
	c := &controller{b: b, known: map[string]bool{}}
	for {
		select {
		case <-b.stop:
			if c.active != nil {
				c.end("shutdown")
			}
			return
		case m := <-b.ctrl:
			c.handle(m)
		case <-c.retryC:
			c.retryC = nil
		}
		c.startNext()
	}
}

func (c *controller) handle(m ctrlMsg) {
	switch m.kind {
	case msgSeen:
		if !c.known[m.participantID] {
			c.known[m.participantID] = true
			c.waiting = append(c.waiting, m.participantID)
			if c.active != nil {
				c.b.log.Info("user waiting; bot is busy", "user", m.participantID, "talkingWith", c.active.userID)
			}
		}
	case msgLeft:
		if c.active != nil && c.active.userID == m.participantID {
			c.end("user_left")
		}
		delete(c.known, m.participantID)
		c.waiting = slices.DeleteFunc(c.waiting, func(id string) bool { return id == m.participantID })
	case msgNovaEnded:
		if c.active != m.conv {
			return
		}
		conv := c.active
		c.end(m.reason)
		if time.Since(conv.startedAt) < healthyConversation {
			c.backOff("conversation ended early", m.reason)
		} else {
			c.retryDelay = 0
		}
		// Still on the Stage, so back of the line for a fresh session.
		c.waiting = append(c.waiting, conv.userID)
	}
}

func (c *controller) startNext() {
	if time.Now().Before(c.retryAt) {
		return // backing off; retryC resumes
	}
	for c.active == nil && len(c.waiting) > 0 {
		id := c.waiting[0]
		c.waiting = c.waiting[1:]
		if !c.known[id] {
			continue
		}
		if !c.start(id) {
			if c.b.ctx.Err() != nil {
				return // shutting down
			}
			c.waiting = append([]string{id}, c.waiting...)
			c.backOff("Nova Sonic unavailable", "")
			return
		}
	}
}

// backOff doubles the delay before the next conversation starts, from
// minRetryDelay up to maxRetryDelay.
func (c *controller) backOff(why, reason string) {
	c.retryDelay = min(max(minRetryDelay, 2*c.retryDelay), maxRetryDelay)
	c.retryAt = time.Now().Add(c.retryDelay)
	c.retryC = time.After(c.retryDelay)
	c.b.log.Warn(why+"; waiting before the next conversation", "reason", reason, "retryIn", c.retryDelay)
}

// start opens a conversation with userID and reports whether it did.
func (c *controller) start(userID string) bool {
	b := c.b
	log := b.log.With("user", userID)
	conv := &conversation{b: b, userID: userID, log: log, startedAt: time.Now()}

	session, err := nova.Dial(b.ctx, b.cfg.Bedrock, nova.Config{
		ModelID:      b.cfg.ModelID,
		VoiceID:      b.cfg.VoiceID,
		SystemPrompt: b.cfg.SystemPrompt,
		SessionLimit: b.cfg.SessionLimit,
	}, nova.Handler{
		OnAudio:      conv.onAudio,
		OnTranscript: conv.onTranscript,
		OnInterrupt:  conv.onInterrupt,
		OnError:      conv.onError,
		OnEnd: func(reason string) {
			select {
			case b.ctrl <- ctrlMsg{kind: msgNovaEnded, conv: conv, reason: reason}:
			case <-b.stop:
			}
		},
	}, log)
	if err != nil {
		if b.ctx.Err() == nil {
			log.Error("could not open a Nova Sonic session", "err", err)
		}
		return false
	}
	conv.session = session
	c.active = conv
	b.nova.Store(session)

	// Subscribe to this user and no one else.
	b.strat.setActive(userID)
	if err := b.stage.RefreshStrategy(); err != nil {
		log.Warn("refresh strategy failed", "err", err)
	}
	log.Info("conversation started")
	return true
}

// end closes the active conversation and unsubscribes from its user.
func (c *controller) end(reason string) {
	conv := c.active
	c.active = nil
	b := c.b

	b.nova.Store(nil)
	conv.session.Close()
	flushed := b.playout.Flush()

	b.strat.setActive("")
	if err := b.stage.RefreshStrategy(); err != nil {
		conv.log.Warn("refresh strategy failed", "err", err)
	}
	conv.log.Info("conversation ended", "reason", reason, "unplayedMs", flushed*1000/nova.SampleRate)
	b.logStats()
}

func (conv *conversation) onAudio(pcm []byte) {
	if !conv.gotAudio {
		conv.gotAudio = true
		conv.log.Info("receiving assistant audio")
	}
	conv.b.novaAudio.Add(int64(len(pcm)))
	if !conv.userTurnAt.IsZero() {
		latency := time.Since(conv.userTurnAt)
		conv.userTurnAt = time.Time{}
		conv.log.Info("time to first audio", "ms", latency.Milliseconds())
	}
	conv.b.playout.Write(pcm)
}

// onTranscript logs what was said. Nova Sonic sends assistant text twice:
// speculative as it starts speaking, then final afterwards; only the first is
// logged.
func (conv *conversation) onTranscript(role, text string, final bool) {
	if role == "USER" {
		conv.userTurnAt = time.Now()
	}
	if role == "ASSISTANT" && final {
		return
	}
	conv.log.Info("transcript", "role", role, "text", text)
}

func (conv *conversation) onInterrupt() {
	n := conv.b.playout.Flush()
	conv.log.Info("barge-in: flushed assistant audio", "ms", n*1000/nova.SampleRate)
}

func (conv *conversation) onError(msg string) {
	conv.log.Warn("nova sonic error", "message", msg)
}
