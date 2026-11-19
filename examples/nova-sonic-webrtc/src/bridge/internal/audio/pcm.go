package audio

import (
	"encoding/binary"
	"fmt"
	"math"
)

// DecodeMono appends interleaved little-endian PCM to dst as mono float32 in
// [-1, 1], averaging channels. bytesPerSample is 2 for signed 16-bit or 4 for
// 32-bit float, the two formats the IVS SDK delivers.
func DecodeMono(dst []float32, data []byte, bytesPerSample, numChannels int) ([]float32, error) {
	if numChannels < 1 {
		return dst, fmt.Errorf("audio: invalid channel count %d", numChannels)
	}
	frameBytes := bytesPerSample * numChannels
	if bytesPerSample != 2 && bytesPerSample != 4 {
		return dst, fmt.Errorf("audio: unsupported bytes per sample %d", bytesPerSample)
	}
	if len(data)%frameBytes != 0 {
		return dst, fmt.Errorf("audio: %d bytes is not a whole number of %d-byte frames", len(data), frameBytes)
	}
	inv := 1 / float32(numChannels)
	for off := 0; off < len(data); off += frameBytes {
		var acc float32
		for c := 0; c < numChannels; c++ {
			p := data[off+c*bytesPerSample:]
			if bytesPerSample == 2 {
				acc += float32(int16(binary.LittleEndian.Uint16(p))) / 32768
			} else {
				acc += math.Float32frombits(binary.LittleEndian.Uint32(p))
			}
		}
		dst = append(dst, acc*inv)
	}
	return dst, nil
}

// FloatToS16 appends src to dst as signed 16-bit samples, rounding and clipping.
func FloatToS16(dst []int16, src []float32) []int16 {
	for _, v := range src {
		s := math.Round(float64(v) * 32768)
		dst = append(dst, int16(max(-32768, min(32767, s))))
	}
	return dst
}

// S16ToBytes appends samples to dst as little-endian bytes.
func S16ToBytes(dst []byte, samples []int16) []byte {
	for _, s := range samples {
		dst = binary.LittleEndian.AppendUint16(dst, uint16(s))
	}
	return dst
}

// BytesToS16 appends little-endian 16-bit PCM to dst. A trailing odd byte is
// ignored.
func BytesToS16(dst []int16, data []byte) []int16 {
	for i := 0; i+1 < len(data); i += 2 {
		dst = append(dst, int16(binary.LittleEndian.Uint16(data[i:])))
	}
	return dst
}

// RMS returns the root mean square of samples, in the same units as the
// samples.
func RMS(samples []int16) float64 {
	if len(samples) == 0 {
		return 0
	}
	var sum float64
	for _, s := range samples {
		sum += float64(s) * float64(s)
	}
	return math.Sqrt(sum / float64(len(samples)))
}
