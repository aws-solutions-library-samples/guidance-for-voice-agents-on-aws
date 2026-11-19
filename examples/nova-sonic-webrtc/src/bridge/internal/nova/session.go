package nova

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/types"
	"github.com/aws/smithy-go"
)

const (
	// openTimeout bounds how long Bedrock may take to open the stream.
	openTimeout = 20 * time.Second
	// closeTimeout bounds sending the closing events.
	closeTimeout = 3 * time.Second
	// sendQueue holds about 4 s of 32 ms frames.
	sendQueue = 128
)

var (
	// closeWait bounds all of Close, so a wedged stream can't hang shutdown.
	closeWait = closeTimeout + 2*time.Second
	// writeFailGrace is how long the writer waits, after a failed send, for the
	// reader to report why the stream ended before ending the session itself.
	writeFailGrace = time.Second
)

// Handler receives what the model says. Every callback except OnEnd runs on the
// session's single reader goroutine, in order. Nil callbacks are skipped.
type Handler struct {
	OnAudio      func(pcm []byte)
	OnTranscript func(role, text string, final bool)
	OnInterrupt  func()
	OnError      func(message string)
	// OnEnd is called at most once when the session ends for any reason other
	// than Close: the model ended the stream (model_ended), Nova Sonic heard no
	// speech for about 295 s (idle_timeout), the stream failed (stream_error or
	// write_error), or the session limit passed (session_limit).
	OnEnd func(reason string)
}

// Config configures one session.
type Config struct {
	ModelID      string
	VoiceID      string
	SystemPrompt string
	// SessionLimit ends the session before Bedrock's 8-minute stream limit.
	// Zero means no limit.
	SessionLimit time.Duration
}

// eventStream is the part of the SDK's bidirectional stream the session uses,
// so tests can substitute a fake.
type eventStream interface {
	Send(ctx context.Context, event types.InvokeModelWithBidirectionalStreamInput) error
	Events() <-chan types.InvokeModelWithBidirectionalStreamOutput
	Close() error
	Err() error
}

type openFunc func(ctx context.Context, modelID string) (eventStream, error)

// Session is one conversation with Nova Sonic over a Bedrock bidirectional
// stream.
type Session struct {
	cfg    Config
	h      Handler
	log    *slog.Logger
	stream eventStream

	prompt       string
	audioContent string

	ctx    context.Context // the stream's lifetime; Close cancels it
	cancel context.CancelFunc

	sendMu     sync.Mutex
	out        chan []byte
	stopWrite  chan struct{}
	writerDone chan struct{}
	readerDone chan struct{}
	limit      *time.Timer

	closing    atomic.Bool
	streamDone atomic.Bool // the reader saw the stream end
	endOnce    sync.Once

	// DroppedAudio counts frames discarded because the send queue was full.
	DroppedAudio atomic.Int64
}

// Dial opens a Nova Sonic session: it starts the bidirectional stream, sends
// the session, prompt, and system-prompt events, and opens the audio input.
func Dial(ctx context.Context, client *bedrockruntime.Client, cfg Config, h Handler, log *slog.Logger) (*Session, error) {
	return dial(ctx, func(ctx context.Context, modelID string) (eventStream, error) {
		out, err := client.InvokeModelWithBidirectionalStream(ctx, &bedrockruntime.InvokeModelWithBidirectionalStreamInput{
			ModelId: aws.String(modelID),
		})
		if err != nil {
			return nil, err
		}
		return out.GetStream(), nil
	}, cfg, h, log)
}

func dial(ctx context.Context, open openFunc, cfg Config, h Handler, log *slog.Logger) (*Session, error) {
	// The stream runs on its own context: the caller's ctx and the open timeout
	// bound only the opening, not the conversation.
	runCtx, cancel := context.WithCancel(context.Background())
	type opened struct {
		stream eventStream
		err    error
	}
	ch := make(chan opened, 1)
	go func() {
		s, err := open(runCtx, cfg.ModelID)
		ch <- opened{s, err}
	}()
	var stream eventStream
	select {
	case o := <-ch:
		if o.err != nil {
			cancel()
			return nil, fmt.Errorf("nova: open stream: %s", describe(o.err))
		}
		stream = o.stream
	case <-time.After(openTimeout):
		cancel()
		return nil, fmt.Errorf("nova: Bedrock did not open the stream within %s (model %s)", openTimeout, cfg.ModelID)
	case <-ctx.Done():
		cancel()
		return nil, ctx.Err()
	}

	s := &Session{
		cfg:          cfg,
		h:            h,
		log:          log,
		stream:       stream,
		prompt:       newID(),
		audioContent: newID(),
		ctx:          runCtx,
		cancel:       cancel,
		out:          make(chan []byte, sendQueue),
		stopWrite:    make(chan struct{}),
		writerDone:   make(chan struct{}),
		readerDone:   make(chan struct{}),
	}
	go s.readLoop()

	system := newID()
	for _, ev := range []object{
		sessionStart(),
		promptStart(s.prompt, cfg.VoiceID),
		systemContentStart(s.prompt, system),
		textInput(s.prompt, system, cfg.SystemPrompt),
		contentEnd(s.prompt, system),
		audioContentStart(s.prompt, s.audioContent),
	} {
		if err := s.send(s.ctx, ev); err != nil {
			s.closing.Store(true)
			stream.Close()
			cancel()
			<-s.readerDone
			if serr := stream.Err(); serr != nil {
				err = serr
			}
			return nil, fmt.Errorf("nova: start session: %s", describe(err))
		}
	}
	go s.writeLoop()
	if cfg.SessionLimit > 0 {
		s.limit = time.AfterFunc(cfg.SessionLimit, func() { s.end("session_limit") })
	}
	log.Info("nova sonic session started", "model", cfg.ModelID, "voice", cfg.VoiceID)
	return s, nil
}

// SendAudio queues one frame of 24 kHz mono S16LE PCM without blocking. If the
// queue is full, the frame is dropped and counted.
func (s *Session) SendAudio(pcm []byte) {
	if s.closing.Load() {
		return
	}
	select {
	case s.out <- pcm:
	default:
		s.DroppedAudio.Add(1)
	}
}

// Close closes the audio input, prompt, and session in the order Nova Sonic
// requires, then the stream, and waits for the session's goroutines, for at
// most closeWait. OnEnd is not called for a session ended by Close. It is safe
// to call more than once.
func (s *Session) Close() {
	if !s.closing.CompareAndSwap(false, true) {
		return
	}
	if s.limit != nil {
		s.limit.Stop()
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		s.shutdown()
	}()
	select {
	case <-done:
	case <-time.After(closeWait):
		s.cancel()
		s.log.Warn("nova sonic stream did not close in time; abandoning it", "waited", closeWait)
	}
}

func (s *Session) shutdown() {
	close(s.stopWrite)
	<-s.writerDone

	ctx, cancel := context.WithTimeout(s.ctx, closeTimeout)
	for _, ev := range []object{contentEnd(s.prompt, s.audioContent), promptEnd(s.prompt), sessionEnd()} {
		if err := s.send(ctx, ev); err != nil {
			break // the stream is already gone
		}
	}
	cancel()
	s.stream.Close()
	s.cancel()
	<-s.readerDone
}

func (s *Session) send(ctx context.Context, ev object) error {
	b, err := json.Marshal(ev)
	if err != nil {
		return err
	}
	s.sendMu.Lock()
	defer s.sendMu.Unlock()
	return s.stream.Send(ctx, &types.InvokeModelWithBidirectionalStreamInputMemberChunk{
		Value: types.BidirectionalInputPayloadPart{Bytes: b},
	})
}

func (s *Session) writeLoop() {
	defer close(s.writerDone)
	for {
		select {
		case <-s.stopWrite:
			return
		case pcm := <-s.out:
			if err := s.send(s.ctx, audioInput(s.prompt, s.audioContent, pcm)); err != nil {
				s.writeFailed(err)
				<-s.stopWrite
				return
			}
		}
	}
}

// writeFailed ends the session after a failed send. Usually the whole stream
// has failed and the reader reports why, so the writer gives it a moment first.
// If only the send side broke, the reader never notices, and the writer ends
// the session itself.
func (s *Session) writeFailed(err error) {
	if s.closing.Load() || s.streamDone.Load() {
		return
	}
	msg := describe(err)
	s.log.Warn("nova sonic send failed", "err", msg)
	select {
	case <-s.readerDone:
	case <-s.stopWrite:
	case <-time.After(writeFailGrace):
		s.end("write_error: " + msg)
	}
}

func (s *Session) readLoop() {
	defer close(s.readerDone)
	p := newParser()
	for ev := range s.stream.Events() {
		chunk, ok := ev.(*types.InvokeModelWithBidirectionalStreamOutputMemberChunk)
		if !ok {
			s.log.Debug("ignoring stream event", "type", fmt.Sprintf("%T", ev))
			continue
		}
		outs, err := p.parse(chunk.Value.Bytes)
		if err != nil {
			s.log.Warn("nova sonic sent an unreadable event", "err", err)
			continue
		}
		for _, o := range outs {
			s.dispatch(o)
		}
	}
	s.streamDone.Store(true)
	if s.closing.Load() {
		return
	}
	if err := s.stream.Err(); err != nil && !errors.Is(err, context.Canceled) {
		msg := describe(err)
		if isIdleTimeout(err) {
			// Expected when the user stays quiet: a fresh session follows.
			s.log.Info("nova sonic ended the stream after about 295 s without speech", "reason", "idle_timeout", "err", msg)
			s.end("idle_timeout")
			return
		}
		s.log.Error("nova sonic stream failed", "err", msg)
		if s.h.OnError != nil {
			s.h.OnError(msg)
		}
		s.end("stream_error: " + msg)
		return
	}
	s.end("model_ended")
}

func (s *Session) dispatch(o output) {
	h := s.h
	switch o.kind {
	case outputAudio:
		if h.OnAudio != nil {
			h.OnAudio(o.pcm)
		}
	case outputTranscript:
		if h.OnTranscript != nil {
			h.OnTranscript(o.role, o.text, o.final)
		}
	case outputInterrupt:
		if h.OnInterrupt != nil {
			h.OnInterrupt()
		}
	}
}

func (s *Session) end(reason string) {
	if s.closing.Load() || s.h.OnEnd == nil {
		return
	}
	s.endOnce.Do(func() { s.h.OnEnd(reason) })
}

// describe names an error's type and message. Bedrock's modeled errors, such as
// AccessDeniedException or ValidationException, carry their code separately.
func describe(err error) string {
	var api smithy.APIError
	if errors.As(err, &api) {
		if msg := api.ErrorMessage(); msg != "" {
			return api.ErrorCode() + ": " + msg
		}
		return api.ErrorCode()
	}
	return err.Error()
}

// isIdleTimeout reports whether err is Nova Sonic ending the stream because it
// received no speech or interactive content for about 295 s. Silent audio
// frames don't reset that timer.
func isIdleTimeout(err error) bool {
	var api smithy.APIError
	if !errors.As(err, &api) || api.ErrorCode() != "ValidationException" {
		return false
	}
	msg := api.ErrorMessage()
	return strings.Contains(msg, "Timed out waiting for audio bytes") ||
		strings.Contains(msg, "gaps between audio bytes")
}

func newID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
