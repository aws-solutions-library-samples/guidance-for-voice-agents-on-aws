package audio

import (
	"encoding/binary"
	"fmt"
	"math"
	"path/filepath"
	"testing"
	"time"
)

func sine(rate, n int, hz, amp float64) []float32 {
	out := make([]float32, n)
	for i := range out {
		out[i] = float32(amp * math.Sin(2*math.Pi*hz*float64(i)/float64(rate)))
	}
	return out
}

// zeroCrossingHz estimates the frequency of a sine from its sign changes.
func zeroCrossingHz(x []float32, rate int) float64 {
	crossings := 0
	for i := 1; i < len(x); i++ {
		if (x[i-1] < 0) != (x[i] < 0) {
			crossings++
		}
	}
	return float64(crossings) / 2 / (float64(len(x)) / float64(rate))
}

func peak(x []float32) float64 {
	var p float64
	for _, v := range x {
		p = max(p, math.Abs(float64(v)))
	}
	return p
}

func TestResamplerTo24k(t *testing.T) {
	for _, inRate := range []int{48000, 44100} {
		t.Run(fmt.Sprint(inRate), func(t *testing.T) {
			in := sine(inRate, inRate*2, 1000, 0.5) // 2 s of 1 kHz
			out := NewResampler(inRate, 24000).Process(nil, in)

			if want := 48000; abs(len(out)-want) > 2 {
				t.Fatalf("got %d samples, want about %d", len(out), want)
			}
			steady := out[2400 : len(out)-2400] // skip filter warm-up
			if hz := zeroCrossingHz(steady, 24000); math.Abs(hz-1000) > 5 {
				t.Errorf("frequency %.1f Hz, want 1000", hz)
			}
			if p := peak(steady); math.Abs(p-0.5) > 0.01 {
				t.Errorf("peak %.4f, want 0.5 (passband gain)", p)
			}
		})
	}
}

func TestResamplerRejectsAliases(t *testing.T) {
	// 15 kHz is above the 12 kHz output Nyquist and must be filtered, not folded
	// down to 9 kHz.
	in := sine(48000, 48000, 15000, 0.5)
	out := NewResampler(48000, 24000).Process(nil, in)
	if p := peak(out[2400 : len(out)-2400]); p > 0.01 {
		t.Errorf("alias peak %.4f, want < 0.01", p)
	}
}

func TestResamplerChunkingIsExact(t *testing.T) {
	for _, inRate := range []int{48000, 44100} {
		in := sine(inRate, inRate, 440, 0.8)
		whole := NewResampler(inRate, 24000).Process(nil, in)

		rs := NewResampler(inRate, 24000)
		var pieces []float32
		sizes := []int{1, 7, 441, 480, 33, 1000, 2}
		for off, i := 0, 0; off < len(in); i++ {
			n := min(sizes[i%len(sizes)], len(in)-off)
			pieces = rs.Process(pieces, in[off:off+n])
			off += n
		}
		if len(pieces) != len(whole) {
			t.Fatalf("%d Hz: chunked produced %d samples, whole %d", inRate, len(pieces), len(whole))
		}
		for i := range whole {
			if pieces[i] != whole[i] {
				t.Fatalf("%d Hz: sample %d differs: %v vs %v", inRate, i, pieces[i], whole[i])
			}
		}
	}
}

func TestResamplerPassthrough(t *testing.T) {
	in := []float32{0.1, -0.2, 0.3}
	out := NewResampler(24000, 24000).Process(nil, in)
	if len(out) != 3 || out[1] != -0.2 {
		t.Errorf("passthrough changed samples: %v", out)
	}
}

func TestDecodeMono(t *testing.T) {
	s16 := S16ToBytes(nil, []int16{16384, -16384, 32767, 32767})
	got, err := DecodeMono(nil, s16, 2, 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0] != 0 || math.Abs(float64(got[1])-1) > 1e-3 {
		t.Errorf("S16 stereo downmix: %v", got)
	}

	var f32 []byte
	for _, v := range []float32{0.25, 0.75} {
		f32 = binary.LittleEndian.AppendUint32(f32, math.Float32bits(v))
	}
	got, err = DecodeMono(nil, f32, 4, 1)
	if err != nil || len(got) != 2 || got[0] != 0.25 || got[1] != 0.75 {
		t.Errorf("F32 mono: %v, %v", got, err)
	}

	if _, err := DecodeMono(nil, []byte{1, 2, 3}, 2, 1); err == nil {
		t.Error("partial frame accepted")
	}
	if _, err := DecodeMono(nil, f32, 3, 1); err == nil {
		t.Error("3 bytes per sample accepted")
	}
}

func TestFloatToS16Clips(t *testing.T) {
	got := FloatToS16(nil, []float32{2, -2, 0})
	if got[0] != 32767 || got[1] != -32768 || got[2] != 0 {
		t.Errorf("got %v", got)
	}
}

func TestUplinkFrames(t *testing.T) {
	var frames [][]byte
	u := NewUplink(24000, 768, func(f []byte) { frames = append(frames, f) })

	// One second of 48 kHz stereo F32 in 10 ms buffers, as the SDK might deliver.
	mono := sine(48000, 48000, 1000, 0.5)
	for off := 0; off < len(mono); off += 480 {
		var buf []byte
		for _, v := range mono[off : off+480] {
			buf = binary.LittleEndian.AppendUint32(buf, math.Float32bits(v))
			buf = binary.LittleEndian.AppendUint32(buf, math.Float32bits(v))
		}
		if err := u.Process(InputFrame{Data: buf, BytesPerSample: 4, NumChannels: 2, SampleRate: 48000}); err != nil {
			t.Fatal(err)
		}
	}
	if len(frames) != 24000/768 {
		t.Errorf("got %d frames, want %d", len(frames), 24000/768)
	}
	for _, f := range frames {
		if len(f) != 768*2 {
			t.Fatalf("frame of %d bytes, want %d", len(f), 768*2)
		}
	}

	// A rate change rebuilds the resampler instead of mis-converting.
	if err := u.Process(InputFrame{Data: make([]byte, 441*2), BytesPerSample: 2, NumChannels: 1, SampleRate: 44100}); err != nil {
		t.Fatal(err)
	}
	if u.RateChanges != 1 {
		t.Errorf("RateChanges = %d, want 1", u.RateChanges)
	}

	n := len(frames)
	u.Silence()
	if len(frames) != n+1 || len(frames[n]) != 768*2 || RMS(BytesToS16(nil, frames[n])) != 0 {
		t.Error("Silence did not emit one silent frame")
	}
}

type push struct {
	frames int
	pts    int64
	rms    float64
}

type fakeSink struct{ pushes []push }

func (s *fakeSink) PushBuffer(data []byte, numFrames int, pts int64) error {
	s.pushes = append(s.pushes, push{numFrames, pts, RMS(BytesToS16(nil, data))})
	return nil
}

func TestPlayoutPacing(t *testing.T) {
	sink := &fakeSink{}
	p := NewPlayout(sink, 24000)

	// 100 ms of audio arrives at once.
	tone := FloatToS16(nil, sine(24000, 2400, 500, 0.5))
	p.Write(S16ToBytes(nil, tone))

	p.advance(10 * time.Millisecond)
	if len(sink.pushes) != 0 {
		t.Fatalf("pushed %d frames before the first 20 ms elapsed", len(sink.pushes))
	}
	p.advance(200 * time.Millisecond)
	if len(sink.pushes) != 10 {
		t.Fatalf("after 200 ms pushed %d frames, want 10", len(sink.pushes))
	}
	for i, ps := range sink.pushes {
		if ps.frames != 480 || ps.pts != int64(i*480) {
			t.Errorf("push %d: %d frames at pts %d", i, ps.frames, ps.pts)
		}
		audible := ps.rms > 1000
		if want := i < 5; audible != want {
			t.Errorf("push %d audible=%v, want %v", i, audible, want)
		}
	}
	if s := p.Stats(); s.SilentFrames != 5*480 || s.Buffered != 0 {
		t.Errorf("stats %+v", s)
	}
}

func TestPlayoutFlushAndSkipAhead(t *testing.T) {
	sink := &fakeSink{}
	p := NewPlayout(sink, 24000)
	p.Write(make([]byte, 24000*2))
	if n := p.Flush(); n != 24000 {
		t.Errorf("Flush discarded %d samples, want 24000", n)
	}
	if p.Stats().Buffered != 0 {
		t.Error("audio still buffered after Flush")
	}

	// A five-second stall skips ahead instead of bursting 250 frames.
	p.advance(5 * time.Second)
	if len(sink.pushes) != 1 || sink.pushes[0].pts != 5*24000-480 {
		t.Errorf("after stall: %d pushes, first pts %d", len(sink.pushes), sink.pushes[0].pts)
	}
}

func TestPlayoutCap(t *testing.T) {
	p := NewPlayout(&fakeSink{}, 8000)
	p.Write(make([]byte, 8000*2*125)) // 125 s into a 120 s buffer
	if s := p.Stats(); s.DroppedSamples != 8000*5 || s.Buffered != 120*time.Second {
		t.Errorf("stats %+v", s)
	}
}

func TestWAVRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "a.wav")
	in := []int16{0, 1000, -1000, 32767}
	if err := WriteWAV(path, 24000, in); err != nil {
		t.Fatal(err)
	}
	w, err := ReadWAV(path)
	if err != nil {
		t.Fatal(err)
	}
	if w.SampleRate != 24000 || len(w.Samples) != 4 || w.Samples[2] != -1000 || w.Samples[3] != 32767 {
		t.Errorf("got %+v", w)
	}
}

func abs(x int) int {
	if x < 0 {
		return -x
	}
	return x
}
