// Command bridge connects users on an IVS Stage to Amazon Nova Sonic. At
// startup it joins the Stage as a bot participant with the IVS Real-Time server
// SDK, using the bot token you supply. It then talks with each user who joins
// the Stage, one at a time, streaming their audio straight to Nova Sonic over
// Bedrock's bidirectional stream and publishing the replies, and logs each
// conversation.
//
// The bridge opens no network listener. Its only AWS call is
// InvokeModelWithBidirectionalStream, made with the default credential chain
// (on EC2, the instance role).
package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	ivsrealtime "github.com/aws/amazon-ivs-realtime-go"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"

	"ivsnovasonic/bridge/internal/bot"
)

// bedrockStreamLimit is Bedrock's 8-minute limit on one bidirectional stream,
// in seconds. SESSION_LIMIT_SECONDS must be shorter so the bridge ends each
// session cleanly first.
const bedrockStreamLimit = 480

const defaultSystemPrompt = "You are a friendly assistant talking with the user over a live audio call. " +
	"Keep your answers short and conversational, usually two or three sentences."

type settings struct {
	botToken     string
	modelID      string
	systemPrompt string
	voiceID      string
	sessionLimit time.Duration
}

func loadSettings(log *slog.Logger) (settings, error) {
	s := settings{
		modelID:      envOr("NOVA_MODEL_ID", "amazon.nova-2-5-sonic"),
		systemPrompt: envOr("SYSTEM_PROMPT", defaultSystemPrompt),
		voiceID:      envOr("VOICE_ID", "matthew"),
	}
	limit, err := strconv.Atoi(envOr("SESSION_LIMIT_SECONDS", "465"))
	if err != nil || limit <= 0 {
		return s, fmt.Errorf("SESSION_LIMIT_SECONDS must be a positive number of seconds")
	}
	if limit >= bedrockStreamLimit {
		return s, fmt.Errorf("SESSION_LIMIT_SECONDS is %d but must be less than %d, Bedrock's 8-minute stream limit", limit, bedrockStreamLimit)
	}
	s.sessionLimit = time.Duration(limit) * time.Second
	token, err := botToken(log)
	if err != nil {
		return s, err
	}
	s.botToken = token
	return s, nil
}

// botToken reads the bot's participant token from BOT_TOKEN_FILE if set, else
// from BOT_TOKEN. A file keeps the token out of the process environment. It
// warns if other users can read the file.
func botToken(log *slog.Logger) (string, error) {
	if path := os.Getenv("BOT_TOKEN_FILE"); path != "" {
		if info, err := os.Stat(path); err == nil && info.Mode().Perm()&0o077 != 0 {
			log.Warn("BOT_TOKEN_FILE is accessible to other users; run chmod 600 on it", "file", path, "mode", info.Mode().Perm().String())
		}
		b, err := os.ReadFile(path)
		if err != nil {
			return "", fmt.Errorf("read BOT_TOKEN_FILE: %w", err)
		}
		if t := strings.TrimSpace(string(b)); t != "" {
			return t, nil
		}
		return "", fmt.Errorf("BOT_TOKEN_FILE %s is empty", path)
	}
	if t := strings.TrimSpace(os.Getenv("BOT_TOKEN")); t != "" {
		os.Unsetenv("BOT_TOKEN")
		return t, nil
	}
	return "", fmt.Errorf("set BOT_TOKEN_FILE or BOT_TOKEN to the bot's participant token (PUBLISH, SUBSCRIBE, attribute role=bot)")
}

func main() {
	log := slog.New(slog.NewTextHandler(os.Stderr, nil))
	if err := run(log); err != nil {
		log.Error("bridge failed", "err", err)
		os.Exit(1)
	}
}

func run(log *slog.Logger) error {
	s, err := loadSettings(log)
	if err != nil {
		return err
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// The Region comes from configuration only: AWS_REGION, or the region in
	// the shared AWS config. There is no built-in default.
	awsCfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		return fmt.Errorf("load AWS config: %w", err)
	}
	if awsCfg.Region == "" {
		return fmt.Errorf("no AWS Region configured: set AWS_REGION to a Region where Nova 2.5 Sonic is available")
	}

	log.Info("joining the Stage as the bot", "ivsSDK", ivsrealtime.Version(), "model", s.modelID, "region", awsCfg.Region)
	b, err := bot.Start(bot.Config{
		BotToken:     s.botToken,
		Bedrock:      bedrockruntime.NewFromConfig(awsCfg),
		ModelID:      s.modelID,
		SystemPrompt: s.systemPrompt,
		VoiceID:      s.voiceID,
		SessionLimit: s.sessionLimit,
	}, log)
	if err != nil {
		return err
	}
	log.Info("bridge ready; waiting for users to join the Stage")

	var runErr error
	select {
	case err := <-b.Fatal():
		runErr = fmt.Errorf("bot lost the Stage; restart the bridge with a fresh BOT_TOKEN: %w", err)
	case <-ctx.Done():
	}
	log.Info("shutting down")
	b.Close()
	return runErr
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
