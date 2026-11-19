package nova

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/types"
	"github.com/aws/smithy-go"
)

func eventName(t *testing.T, ev object) (string, object) {
	t.Helper()
	inner := ev["event"].(object)
	if len(inner) != 1 {
		t.Fatalf("event has %d keys: %v", len(inner), inner)
	}
	for k, v := range inner {
		return k, v.(object)
	}
	return "", nil
}

func TestEventsUse24kHzBothWays(t *testing.T) {
	_, ps := eventName(t, promptStart("p", "tiffany"))
	out := ps["audioOutputConfiguration"].(object)
	if out["sampleRateHertz"] != 24000 || out["voiceId"] != "tiffany" || out["channelCount"] != 1 || out["sampleSizeBits"] != 16 {
		t.Errorf("audio output config %v", out)
	}
	_, cs := eventName(t, audioContentStart("p", "a"))
	if cs["type"] != "AUDIO" || cs["role"] != "USER" || cs["audioInputConfiguration"].(object)["sampleRateHertz"] != 24000 {
		t.Errorf("audio content start %v", cs)
	}
	_, ss := eventName(t, sessionStart())
	if _, ok := ss["turnDetectionConfiguration"]; !ok {
		t.Error("turnDetectionConfiguration must be inside sessionStart")
	}
	_, ai := eventName(t, audioInput("p", "a", []byte{1, 2}))
	if ai["content"] != base64.StdEncoding.EncodeToString([]byte{1, 2}) {
		t.Errorf("audio input %v", ai)
	}
	if _, err := json.Marshal(promptStart("p", "matthew")); err != nil {
		t.Error(err)
	}
}

func TestAudioInputAndOutputConfigsAreSeparate(t *testing.T) {
	_, cs := eventName(t, audioContentStart("p", "a"))
	in := cs["audioInputConfiguration"].(object)
	if _, ok := in["voiceId"]; ok {
		t.Errorf("audio input config has a voiceId: %v", in)
	}
	in["sampleRateHertz"] = 16000 // must not leak into later events
	_, ps := eventName(t, promptStart("p", "tiffany"))
	_, cs = eventName(t, audioContentStart("p", "a"))
	if ps["audioOutputConfiguration"].(object)["sampleRateHertz"] != 24000 || cs["audioInputConfiguration"].(object)["sampleRateHertz"] != 24000 {
		t.Error("audio configs share state between events")
	}
}

func raw(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func contentStartEvent(id, role, stage string) object {
	cs := object{"contentId": id, "role": role, "type": "TEXT"}
	if stage != "" {
		cs["additionalModelFields"] = `{"generationStage":"` + stage + `"}`
	}
	return event("contentStart", cs)
}

func TestParserLabelsTranscripts(t *testing.T) {
	p := newParser()
	if outs, _ := p.parse(raw(t, contentStartEvent("u1", "USER", "FINAL"))); len(outs) != 0 {
		t.Errorf("contentStart produced %v", outs)
	}
	outs, _ := p.parse(raw(t, event("textOutput", object{"contentId": "u1", "content": "hi there"})))
	if len(outs) != 1 || outs[0].kind != outputTranscript || outs[0].role != "USER" || outs[0].text != "hi there" || !outs[0].final {
		t.Errorf("user transcript %+v", outs)
	}

	p.parse(raw(t, contentStartEvent("a1", "ASSISTANT", "SPECULATIVE")))
	outs, _ = p.parse(raw(t, event("textOutput", object{"contentId": "a1", "role": "ASSISTANT", "content": "Paris."})))
	if len(outs) != 1 || outs[0].final {
		t.Errorf("speculative assistant text marked final: %+v", outs)
	}
	p.parse(raw(t, contentStartEvent("a2", "ASSISTANT", "FINAL")))
	outs, _ = p.parse(raw(t, event("textOutput", object{"contentId": "a2", "content": "Paris."})))
	if len(outs) != 1 || !outs[0].final || outs[0].role != "ASSISTANT" {
		t.Errorf("final assistant text %+v", outs)
	}
}

func TestParserDecodesAudio(t *testing.T) {
	outs, err := newParser().parse(raw(t, event("audioOutput", object{"content": base64.StdEncoding.EncodeToString([]byte{7, 0})})))
	if err != nil || len(outs) != 1 || outs[0].kind != outputAudio || len(outs[0].pcm) != 2 || outs[0].pcm[0] != 7 {
		t.Errorf("audio %+v %v", outs, err)
	}
}

func TestParserDetectsBargeInBothWays(t *testing.T) {
	p := newParser()
	p.parse(raw(t, contentStartEvent("a1", "ASSISTANT", "FINAL")))
	if outs, _ := p.parse(raw(t, event("textOutput", object{"contentId": "a1", "content": `{ "interrupted" : true }`}))); len(outs) != 1 || outs[0].kind != outputInterrupt {
		t.Errorf("interrupted text: %+v", outs)
	}
	if outs, _ := p.parse(raw(t, event("contentEnd", object{"contentId": "a1", "stopReason": "INTERRUPTED"}))); len(outs) != 1 || outs[0].kind != outputInterrupt {
		t.Errorf("INTERRUPTED stop reason: %+v", outs)
	}
	if outs, _ := p.parse(raw(t, event("contentEnd", object{"contentId": "x", "stopReason": "END_TURN"}))); len(outs) != 0 {
		t.Errorf("END_TURN: %+v", outs)
	}
}

func TestParserIgnoresOtherEvents(t *testing.T) {
	p := newParser()
	for _, ev := range []object{event("usageEvent", object{}), event("completionStart", object{}), {}} {
		if outs, err := p.parse(raw(t, ev)); err != nil || len(outs) != 0 {
			t.Errorf("%v: %+v %v", ev, outs, err)
		}
	}
	if _, err := p.parse([]byte("not json")); err == nil {
		t.Error("invalid JSON accepted")
	}
}

// fakeStream records what the session sends and delivers scripted events.
type fakeStream struct {
	mu        sync.Mutex
	sent      []string // event names, in order
	audio     int
	events    chan types.InvokeModelWithBidirectionalStreamOutput
	closeOnce sync.Once
	err       error
	sendErr   error
	wedged    bool // Close leaves the events channel open
}

func newFakeStream() *fakeStream {
	return &fakeStream{events: make(chan types.InvokeModelWithBidirectionalStreamOutput, 16)}
}

func (f *fakeStream) Send(_ context.Context, ev types.InvokeModelWithBidirectionalStreamInput) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.sendErr != nil {
		return f.sendErr
	}
	chunk := ev.(*types.InvokeModelWithBidirectionalStreamInputMemberChunk)
	var env map[string]map[string]any
	if err := json.Unmarshal(chunk.Value.Bytes, &env); err != nil {
		return err
	}
	for name := range env["event"] {
		f.sent = append(f.sent, name)
		if name == "audioInput" {
			f.audio++
		}
	}
	return nil
}

func (f *fakeStream) Events() <-chan types.InvokeModelWithBidirectionalStreamOutput { return f.events }

// Close ends the events channel, as the SDK does, unless the stream is wedged.
func (f *fakeStream) Close() error {
	f.mu.Lock()
	wedged := f.wedged
	f.mu.Unlock()
	if !wedged {
		f.end()
	}
	return nil
}
func (f *fakeStream) end()       { f.closeOnce.Do(func() { close(f.events) }) }
func (f *fakeStream) Err() error { return f.err }

func (f *fakeStream) emit(t *testing.T, ev object) {
	f.events <- &types.InvokeModelWithBidirectionalStreamOutputMemberChunk{
		Value: types.BidirectionalOutputPayloadPart{Bytes: raw(t, ev)},
	}
}

func (f *fakeStream) names() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.sent...)
}

var discard = slog.New(slog.NewTextHandler(io.Discard, nil))

func dialFake(t *testing.T, f *fakeStream, cfg Config, h Handler) *Session {
	t.Helper()
	s, err := dial(context.Background(), func(context.Context, string) (eventStream, error) { return f, nil }, cfg, h, discard)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func equal(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func TestSessionLifecycle(t *testing.T) {
	f := newFakeStream()
	var mu sync.Mutex
	var audio []byte
	var transcripts []string
	interrupts := 0
	ended := make(chan string, 1)
	s := dialFake(t, f, Config{ModelID: "m", VoiceID: "matthew", SystemPrompt: "be brief"}, Handler{
		OnAudio: func(pcm []byte) { mu.Lock(); audio = append(audio, pcm...); mu.Unlock() },
		OnTranscript: func(role, text string, final bool) {
			mu.Lock()
			transcripts = append(transcripts, role+":"+text)
			mu.Unlock()
		},
		OnInterrupt: func() { mu.Lock(); interrupts++; mu.Unlock() },
		OnEnd:       func(reason string) { ended <- reason },
	})

	setup := []string{"sessionStart", "promptStart", "contentStart", "textInput", "contentEnd", "contentStart"}
	if got := f.names(); !equal(got, setup) {
		t.Fatalf("setup events %v, want %v", got, setup)
	}

	s.SendAudio([]byte{1, 0})
	s.SendAudio([]byte{2, 0})
	deadline := time.Now().Add(2 * time.Second)
	for {
		f.mu.Lock()
		n := f.audio
		f.mu.Unlock()
		if n == 2 || time.Now().After(deadline) {
			break
		}
		time.Sleep(time.Millisecond)
	}

	f.emit(t, contentStartEvent("u1", "USER", ""))
	f.emit(t, event("textOutput", object{"contentId": "u1", "content": "hello"}))
	f.emit(t, event("audioOutput", object{"content": base64.StdEncoding.EncodeToString([]byte{5, 0})}))
	f.emit(t, event("contentEnd", object{"contentId": "u1", "stopReason": "INTERRUPTED"}))

	s.Close()
	s.Close() // idempotent

	got := f.names()
	if f.audio != 2 {
		t.Errorf("sent %d audio frames, want 2", f.audio)
	}
	if tail := got[len(got)-3:]; !equal(tail, []string{"contentEnd", "promptEnd", "sessionEnd"}) {
		t.Errorf("closing events %v", tail)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(transcripts) != 1 || transcripts[0] != "USER:hello" || len(audio) != 2 || interrupts != 1 {
		t.Errorf("transcripts %v audio %v interrupts %d", transcripts, audio, interrupts)
	}
	select {
	case r := <-ended:
		t.Errorf("OnEnd(%q) called for a session ended by Close", r)
	default:
	}
}

func TestSessionReportsModelEndAndStreamError(t *testing.T) {
	f := newFakeStream()
	ended := make(chan string, 1)
	s := dialFake(t, f, Config{}, Handler{OnEnd: func(r string) { ended <- r }})
	f.Close()
	if r := <-ended; r != "model_ended" {
		t.Errorf("reason %q, want model_ended", r)
	}
	s.Close()

	f = newFakeStream()
	f.err = &smithy.GenericAPIError{Code: "AccessDeniedException", Message: "no access"}
	var errMsg string
	ended = make(chan string, 1)
	s = dialFake(t, f, Config{}, Handler{OnError: func(m string) { errMsg = m }, OnEnd: func(r string) { ended <- r }})
	f.Close()
	if r := <-ended; r != "stream_error: AccessDeniedException: no access" || errMsg != "AccessDeniedException: no access" {
		t.Errorf("reason %q, error %q", r, errMsg)
	}
	s.Close()
}

// idleTimeoutErr is the error Bedrock returns after about 295 s without speech.
var idleTimeoutErr = &smithy.GenericAPIError{
	Code: "ValidationException",
	Message: "RequestId=00000000-0000-0000-0000-000000000000 : Timed out waiting for audio bytes or interactive content. " +
		"Please ensure gaps between audio bytes and interactive content are less than 295 seconds.",
}

func TestSessionReportsIdleTimeout(t *testing.T) {
	var logs bytes.Buffer
	var logMu sync.Mutex
	log := slog.New(slog.NewTextHandler(&lockedWriter{w: &logs, mu: &logMu}, nil))

	f := newFakeStream()
	f.err = idleTimeoutErr
	ended := make(chan string, 1)
	errored := make(chan string, 1)
	s, err := dial(context.Background(), func(context.Context, string) (eventStream, error) { return f, nil }, Config{}, Handler{
		OnError: func(m string) { errored <- m },
		OnEnd:   func(r string) { ended <- r },
	}, log)
	if err != nil {
		t.Fatal(err)
	}
	f.Close()
	if r := <-ended; r != "idle_timeout" {
		t.Errorf("reason %q, want idle_timeout", r)
	}
	s.Close()
	select {
	case m := <-errored:
		t.Errorf("OnError(%q) called for an idle timeout", m)
	default:
	}
	logMu.Lock()
	out := logs.String()
	logMu.Unlock()
	if strings.Contains(out, "level=ERROR") || !strings.Contains(out, "level=INFO") || !strings.Contains(out, "reason=idle_timeout") {
		t.Errorf("idle timeout logs:\n%s", out)
	}
}

func TestOtherValidationErrorsAreStreamErrors(t *testing.T) {
	f := newFakeStream()
	f.err = &smithy.GenericAPIError{Code: "ValidationException", Message: "Invalid input request"}
	ended := make(chan string, 1)
	errored := make(chan string, 1)
	s := dialFake(t, f, Config{}, Handler{OnError: func(m string) { errored <- m }, OnEnd: func(r string) { ended <- r }})
	f.Close()
	if r := <-ended; r != "stream_error: ValidationException: Invalid input request" {
		t.Errorf("reason %q", r)
	}
	if m := <-errored; m != "ValidationException: Invalid input request" {
		t.Errorf("error %q", m)
	}
	s.Close()

	if isIdleTimeout(errors.New("Timed out waiting for audio bytes")) {
		t.Error("a non-API error counted as an idle timeout")
	}
	if isIdleTimeout(&smithy.GenericAPIError{Code: "ModelTimeoutException", Message: idleTimeoutErr.Message}) {
		t.Error("another error code counted as an idle timeout")
	}
}

func TestSessionEndsOnWriteError(t *testing.T) {
	defer func(d time.Duration) { writeFailGrace = d }(writeFailGrace)
	writeFailGrace = 10 * time.Millisecond

	f := newFakeStream()
	ended := make(chan string, 1)
	s := dialFake(t, f, Config{}, Handler{OnEnd: func(r string) { ended <- r }})
	// Only the send side breaks: the events channel stays open.
	f.mu.Lock()
	f.sendErr = errors.New("broken pipe")
	f.mu.Unlock()
	s.SendAudio([]byte{1, 0})
	select {
	case r := <-ended:
		if r != "write_error: broken pipe" {
			t.Errorf("reason %q", r)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("session never ended after a failed send")
	}
	s.Close()
}

func TestCloseIsBounded(t *testing.T) {
	defer func(d time.Duration) { closeWait = d }(closeWait)
	closeWait = 50 * time.Millisecond

	f := newFakeStream()
	f.wedged = true
	s := dialFake(t, f, Config{}, Handler{})
	defer f.end() // let the reader goroutine exit

	done := make(chan struct{})
	go func() { s.Close(); close(done) }()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Close hung on a wedged stream")
	}
}

type lockedWriter struct {
	w  io.Writer
	mu *sync.Mutex
}

func (l *lockedWriter) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.w.Write(p)
}

func TestSessionLimit(t *testing.T) {
	f := newFakeStream()
	ended := make(chan string, 1)
	s := dialFake(t, f, Config{SessionLimit: 20 * time.Millisecond}, Handler{OnEnd: func(r string) { ended <- r }})
	defer s.Close()
	select {
	case r := <-ended:
		if r != "session_limit" {
			t.Errorf("reason %q", r)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("session limit never fired")
	}
}

func TestSendAudioDropsWhenFull(t *testing.T) {
	f := newFakeStream()
	s := dialFake(t, f, Config{}, Handler{})
	f.mu.Lock() // stall the writer inside Send
	for i := 0; i < sendQueue+10; i++ {
		s.SendAudio([]byte{0, 0})
	}
	f.mu.Unlock()
	if s.DroppedAudio.Load() == 0 {
		t.Error("no frames dropped with a stalled writer")
	}
	s.Close()
}

func TestDialFailures(t *testing.T) {
	_, err := dial(context.Background(), func(context.Context, string) (eventStream, error) {
		return nil, &smithy.GenericAPIError{Code: "ValidationException", Message: "bad model"}
	}, Config{}, Handler{}, discard)
	if err == nil || err.Error() != "nova: open stream: ValidationException: bad model" {
		t.Errorf("open failure: %v", err)
	}

	f := newFakeStream()
	f.sendErr = errors.New("broken pipe")
	if _, err := dial(context.Background(), func(context.Context, string) (eventStream, error) { return f, nil }, Config{}, Handler{}, discard); err == nil {
		t.Error("setup send failure not reported")
	}
}
