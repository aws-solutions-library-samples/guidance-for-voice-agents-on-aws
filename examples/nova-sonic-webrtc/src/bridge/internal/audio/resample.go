// Package audio holds the PCM plumbing between an IVS Stage and Nova Sonic:
// sample conversion, rate conversion, framing, and real-time playout pacing.
// It has no cgo dependency, so it builds and tests on any platform.
package audio

import "math"

// tapsPerOutputPeriod sets filter length. Each output sample sums this many input
// samples per unit of decimation ratio, which gives roughly a 4 kHz transition
// band at the 44.1/48 kHz → 24 kHz conversions this bridge performs.
const tapsPerOutputPeriod = 32

// Resampler converts a mono float32 stream from one sample rate to another with
// a polyphase windowed-sinc filter. It keeps filter history between calls, so a
// stream split into buffers of any size produces exactly the same output as the
// stream processed in one call.
type Resampler struct {
	inRate    int
	up, down  int       // reduced rational ratio outRate/inRate = up/down
	taps      int       // filter taps per polyphase branch
	bank      []float32 // bank[phase*taps+k] is tap k of branch phase
	hist      []float32 // input samples; hist[0] has absolute index histStart
	histStart int64
	inCount   int64 // input samples received so far
	outCount  int64 // output samples produced so far
}

// NewResampler returns a resampler from inRate to outRate in Hz. Equal rates pass
// samples through unchanged.
func NewResampler(inRate, outRate int) *Resampler {
	g := gcd(inRate, outRate)
	r := &Resampler{inRate: inRate, up: outRate / g, down: inRate / g}
	if r.up == 1 && r.down == 1 {
		return r
	}
	ratio := max(1, (r.down+r.up-1)/r.up)
	r.taps = tapsPerOutputPeriod * ratio
	r.bank = designBank(r.up, r.down, r.taps)
	return r
}

// InRate reports the input rate this resampler converts from.
func (r *Resampler) InRate() int { return r.inRate }

// Process appends the resampled form of in to dst and returns it.
func (r *Resampler) Process(dst, in []float32) []float32 {
	if r.bank == nil {
		return append(dst, in...)
	}
	r.hist = append(r.hist, in...)
	r.inCount += int64(len(in))
	up, down, taps := int64(r.up), int64(r.down), r.taps
	for {
		pos := r.outCount * down
		i, phase := pos/up, int(pos%up)
		if i >= r.inCount {
			break
		}
		branch := r.bank[phase*taps : (phase+1)*taps]
		var acc float32
		for k, h := range branch {
			idx := i - int64(k) - r.histStart
			if idx < 0 {
				break // before the start of the stream: implicit zeros
			}
			acc += h * r.hist[idx]
		}
		dst = append(dst, acc)
		r.outCount++
	}
	// Keep only the history the next output sample can reach.
	next := (r.outCount * down) / up
	keepFrom := min(next-int64(taps-1), r.inCount)
	if drop := keepFrom - r.histStart; drop > 0 {
		n := copy(r.hist, r.hist[drop:])
		r.hist = r.hist[:n]
		r.histStart = keepFrom
	}
	return dst
}

// designBank builds a Blackman-windowed sinc low-pass prototype at the
// upsampled rate and splits it into up polyphase branches of taps each. The
// cutoff sits just under the lower of the two Nyquist frequencies.
func designBank(up, down, taps int) []float32 {
	n := up * taps
	cutoff := 0.5 / float64(max(up, down)) * 0.92 // cycles per upsampled sample
	center := float64(n-1) / 2
	proto := make([]float64, n)
	var sum float64
	for j := range proto {
		x := float64(j) - center
		sinc := 2 * cutoff
		if x != 0 {
			sinc = math.Sin(2*math.Pi*cutoff*x) / (math.Pi * x)
		}
		w := 0.42 - 0.5*math.Cos(2*math.Pi*float64(j)/float64(n-1)) + 0.08*math.Cos(4*math.Pi*float64(j)/float64(n-1))
		proto[j] = sinc * w
		sum += proto[j]
	}
	// Unity DC gain per branch after zero-stuffing by up.
	scale := float64(up) / sum
	bank := make([]float32, n)
	for phase := 0; phase < up; phase++ {
		for k := 0; k < taps; k++ {
			bank[phase*taps+k] = float32(proto[phase+k*up] * scale)
		}
	}
	return bank
}

func gcd(a, b int) int {
	for b != 0 {
		a, b = b, a%b
	}
	return a
}
