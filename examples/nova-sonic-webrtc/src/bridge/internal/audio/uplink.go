package audio

import "fmt"

// InputFrame is one buffer of remote participant audio as the IVS SDK delivers
// it: interleaved, any sample rate, S16 or F32, mono or stereo.
type InputFrame struct {
	Data           []byte
	BytesPerSample int
	NumChannels    int
	SampleRate     int
}

// Uplink turns Stage audio into the fixed-size frames Nova Sonic expects:
// mono signed 16-bit little-endian PCM at OutRate, frameSize samples per frame.
// It is not safe for concurrent use; one goroutine owns it.
type Uplink struct {
	outRate   int
	frameSize int
	emit      func(frame []byte)

	rs      *Resampler
	mono    []float32
	resamp  []float32
	pending []int16
	silence []byte

	// RateChanges counts how many times the input rate changed mid-stream.
	RateChanges int
}

// NewUplink returns an Uplink that calls emit with each complete frame. emit
// receives a fresh slice it may retain.
func NewUplink(outRate, frameSize int, emit func(frame []byte)) *Uplink {
	return &Uplink{
		outRate:   outRate,
		frameSize: frameSize,
		emit:      emit,
		silence:   make([]byte, frameSize*2),
	}
}

// Process converts one input buffer and emits every frame it completes.
func (u *Uplink) Process(f InputFrame) error {
	if f.SampleRate <= 0 {
		return fmt.Errorf("audio: invalid sample rate %d", f.SampleRate)
	}
	if u.rs == nil || u.rs.InRate() != f.SampleRate {
		if u.rs != nil {
			u.RateChanges++
		}
		u.rs = NewResampler(f.SampleRate, u.outRate)
	}
	var err error
	u.mono, err = DecodeMono(u.mono[:0], f.Data, f.BytesPerSample, f.NumChannels)
	if err != nil {
		return err
	}
	u.resamp = u.rs.Process(u.resamp[:0], u.mono)
	u.pending = FloatToS16(u.pending, u.resamp)
	start := 0
	for len(u.pending)-start >= u.frameSize {
		u.emit(S16ToBytes(make([]byte, 0, u.frameSize*2), u.pending[start:start+u.frameSize]))
		start += u.frameSize
	}
	n := copy(u.pending, u.pending[start:])
	u.pending = u.pending[:n]
	return nil
}

// Silence emits one frame of silence. Nova Sonic expects audio at a steady
// cadence, so the bridge sends this while the participant delivers nothing.
func (u *Uplink) Silence() {
	u.emit(append([]byte(nil), u.silence...))
}
