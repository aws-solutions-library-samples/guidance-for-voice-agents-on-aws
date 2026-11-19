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

const defaultSystemPrompt = "You are a friendly assistant talking with the user over a live audio call. " +
	"Keep your answers short and conversational, usually two or three sentences."

type settings struct {
	botToken     string
	modelID      string
	systemPrompt string
	voiceID      string
	sessionLimit time.Duration
}

func loadSettings() (settings, error) {
	s := settings{
		modelID:      envOr("NOVA_MODEL_ID", "amazon.nova-2-5-sonic"),
		systemPrompt: envOr("SYSTEM_PROMPT", defaultSystemPrompt),
		voiceID:      envOr("VOICE_ID", "matthew"),
	}
	limit, err := strconv.Atoi(envOr("SESSION_LIMIT_SECONDS", "465"))
	if err != nil || limit <= 0 {
		return s, fmt.Errorf("SESSION_LIMIT_SECONDS must be a positive number of seconds")
	}
	s.sessionLimit = time.Duration(limit) * time.Second
	token, err := botToken()
	if err != nil {
		return s, err
	}
	s.botToken = token
	return s, nil
}

// botToken reads the bot's participant token from BOT_TOKEN_FILE if set, else
// from BOT_TOKEN. A file keeps the token out of the process environment.
func botToken() (string, error) {
	if path := os.Getenv("BOT_TOKEN_FILE"); path != "" {
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
	s, err := loadSettings()
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
