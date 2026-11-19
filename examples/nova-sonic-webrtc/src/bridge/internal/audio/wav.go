package audio

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
)

// WAV is decoded 16-bit PCM from a WAV file, downmixed to mono.
type WAV struct {
	SampleRate int
	Samples    []int16
}

// ReadWAV reads a 16-bit PCM WAV file of any sample rate and channel count and
// downmixes it to mono.
func ReadWAV(path string) (*WAV, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	if len(raw) < 12 || string(raw[0:4]) != "RIFF" || string(raw[8:12]) != "WAVE" {
		return nil, errors.New("audio: not a RIFF/WAVE file")
	}
	var rate, channels, bits int
	var data []byte
	for off := 12; off+8 <= len(raw); {
		id := string(raw[off : off+4])
		size := int(binary.LittleEndian.Uint32(raw[off+4:]))
		body := raw[off+8 : min(len(raw), off+8+size)]
		switch id {
		case "fmt ":
			if len(body) < 16 {
				return nil, errors.New("audio: short fmt chunk")
			}
			format := binary.LittleEndian.Uint16(body[0:])
			if format != 1 && format != 0xFFFE { // PCM or WAVE_FORMAT_EXTENSIBLE
				return nil, fmt.Errorf("audio: unsupported WAV format %d; want PCM", format)
			}
			channels = int(binary.LittleEndian.Uint16(body[2:]))
			rate = int(binary.LittleEndian.Uint32(body[4:]))
			bits = int(binary.LittleEndian.Uint16(body[14:]))
		case "data":
			data = body
		}
		off += 8 + size + size%2
	}
	if bits != 16 {
		return nil, fmt.Errorf("audio: %d-bit WAV; want 16-bit PCM", bits)
	}
	if data == nil || channels == 0 || rate == 0 {
		return nil, errors.New("audio: WAV has no fmt or data chunk")
	}
	data = data[:len(data)-len(data)%(2*channels)]
	mono, err := DecodeMono(nil, data, 2, channels)
	if err != nil {
		return nil, err
	}
	return &WAV{SampleRate: rate, Samples: FloatToS16(nil, mono)}, nil
}

// WriteWAV writes mono 16-bit PCM as a WAV file.
func WriteWAV(path string, sampleRate int, samples []int16) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	if err := writeWAV(f, sampleRate, samples); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

func writeWAV(w io.Writer, sampleRate int, samples []int16) error {
	dataLen := uint32(len(samples) * 2)
	h := make([]byte, 0, 44)
	h = append(h, "RIFF"...)
	h = binary.LittleEndian.AppendUint32(h, 36+dataLen)
	h = append(h, "WAVEfmt "...)
	h = binary.LittleEndian.AppendUint32(h, 16)
	h = binary.LittleEndian.AppendUint16(h, 1) // PCM
	h = binary.LittleEndian.AppendUint16(h, 1) // mono
	h = binary.LittleEndian.AppendUint32(h, uint32(sampleRate))
	h = binary.LittleEndian.AppendUint32(h, uint32(sampleRate*2))
	h = binary.LittleEndian.AppendUint16(h, 2)
	h = binary.LittleEndian.AppendUint16(h, 16)
	h = append(h, "data"...)
	h = binary.LittleEndian.AppendUint32(h, dataLen)
	if _, err := w.Write(h); err != nil {
		return err
	}
	_, err := w.Write(S16ToBytes(make([]byte, 0, dataLen), samples))
	return err
}
