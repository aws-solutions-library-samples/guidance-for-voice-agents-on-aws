// Command nova-check tests Nova Sonic on its own, without IVS: it streams a WAV
// file to the model in real time, as the bridge does, and saves the spoken
// reply. It needs no IVS SDK, so it builds and runs anywhere Go does, given AWS
// credentials allowed bedrock:InvokeModel on the model.
//
// It exits 0 if Nova Sonic replied with audio.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"log/slog"
	"os"
	"sync"
	"time"

	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"

	"ivsnovasonic/bridge/internal/audio"
	"ivsnovasonic/bridge/internal/nova"
)

const frameSamples = 768 // 32 ms at 24 kHz, as the bridge sends

var (
	wavPath = flag.String("wav", "question.wav", "16-bit PCM WAV with a spoken question")
	outPath = flag.String("out", "reply.wav", "where to write Nova Sonic's reply")
	region  = flag.String("region", "", "Bedrock Region (default: AWS_REGION or the shared AWS config)")
	modelID = flag.String("model", envOr("NOVA_MODEL_ID", "amazon.nova-2-5-sonic"), "Nova Sonic model ID")
	voiceID = flag.String("voice", "matthew", "Nova Sonic voice")
	timeout = flag.Duration("timeout", 20*time.Second, "how long to wait for a reply after the question")
)

func main() {
	flag.Parse()
	log.SetFlags(log.Ltime | log.Lmicroseconds)
	if err := run(); err != nil {
		log.Fatalf("FAIL: %v", err)
	}
}

func run() error {
	question, err := loadAt24k(*wavPath)
	if err != nil {
		return err
	}

	ctx := context.Background()
	var opts []func(*config.LoadOptions) error
	if *region != "" {
		opts = append(opts, config.WithRegion(*region))
	}
	cfg, err := config.LoadDefaultConfig(ctx, opts...)
	if err != nil {
		return fmt.Errorf("load AWS config: %w", err)
	}
	if cfg.Region == "" {
		return fmt.Errorf("no AWS Region configured: set AWS_REGION or pass -region")
	}

	var (
		mu         sync.Mutex
		reply      []int16
		firstAudio time.Time
		lastAudio  time.Time
	)
	ended := make(chan string, 1)
	session, err := nova.Dial(ctx, bedrockruntime.NewFromConfig(cfg), nova.Config{
		ModelID:      *modelID,
		VoiceID:      *voiceID,
		SystemPrompt: "You are a friendly assistant. Keep your answers to one or two sentences.",
	}, nova.Handler{
		OnAudio: func(pcm []byte) {
			mu.Lock()
			defer mu.Unlock()
			now := time.Now()
			if firstAudio.IsZero() {
				firstAudio = now
			}
			lastAudio = now
			reply = audio.BytesToS16(reply, pcm)
		},
		OnTranscript: func(role, text string, final bool) {
			if role == "USER" || !final {
				log.Printf("  %s: %s", role, text)
			}
		},
		OnInterrupt: func() { log.Printf("  [barge-in]") },
		OnError:     func(msg string) { log.Printf("  [error] %s", msg) },
		OnEnd:       func(reason string) { ended <- reason },
	}, slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelWarn})))
	if err != nil {
		return err
	}
	defer session.Close()
	log.Printf("session open (model %s, region %s); speaking %s (%.1f s)", *modelID, *region, *wavPath, float64(len(question))/nova.SampleRate)

	// Stream the question in real time, then keep sending silence, as a live
	// microphone would, so Nova Sonic can detect the end of the turn.
	silence := make([]int16, frameSamples)
	start := time.Now()
	var spokeAt time.Time
	deadline := time.Now().Add(time.Duration(len(question))*time.Second/nova.SampleRate + *timeout)
	for i := 0; ; i++ {
		frame := silence
		if off := i * frameSamples; off < len(question) {
			frame = question[off:min(off+frameSamples, len(question))]
			if len(frame) < frameSamples {
				frame = append(append([]int16(nil), frame...), silence[len(frame):]...)
			}
		} else if spokeAt.IsZero() {
			spokeAt = time.Now()
			log.Printf("finished speaking; sending silence")
		}
		session.SendAudio(audio.S16ToBytes(nil, frame))

		mu.Lock()
		done := !lastAudio.IsZero() && time.Since(lastAudio) > 2*time.Second
		mu.Unlock()
		select {
		case reason := <-ended:
			log.Printf("session ended: %s", reason)
			done = true
		default:
		}
		if done || time.Now().After(deadline) {
			break
		}
		time.Sleep(time.Until(start.Add(time.Duration(i+1) * frameSamples * time.Second / nova.SampleRate)))
	}

	mu.Lock()
	defer mu.Unlock()
	if err := audio.WriteWAV(*outPath, nova.SampleRate, reply); err != nil {
		return err
	}
	log.Printf("wrote %s (%.1f s)", *outPath, float64(len(reply))/nova.SampleRate)
	if len(reply) == 0 {
		return fmt.Errorf("no reply audio")
	}
	if !spokeAt.IsZero() && firstAudio.After(spokeAt) {
		log.Printf("time to first audio: %d ms after the question ended", firstAudio.Sub(spokeAt).Milliseconds())
	}
	log.Printf("PASS")
	return nil
}

// loadAt24k reads a 16-bit WAV and converts it to 24 kHz mono.
func loadAt24k(path string) ([]int16, error) {
	w, err := audio.ReadWAV(path)
	if err != nil {
		return nil, err
	}
	if w.SampleRate == nova.SampleRate {
		return w.Samples, nil
	}
	in := make([]float32, len(w.Samples))
	for i, s := range w.Samples {
		in[i] = float32(s) / 32768
	}
	return audio.FloatToS16(nil, audio.NewResampler(w.SampleRate, nova.SampleRate).Process(nil, in)), nil
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
