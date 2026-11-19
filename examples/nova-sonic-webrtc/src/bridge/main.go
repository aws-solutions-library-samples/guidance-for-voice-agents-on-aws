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
	"syscall"

	ivsrealtime "github.com/aws/amazon-ivs-realtime-go"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"

	"ivsnovasonic/bridge/internal/bot"
	"ivsnovasonic/bridge/internal/settings"
)

func main() {
	log := slog.New(slog.NewTextHandler(os.Stderr, nil))
	if err := run(log); err != nil {
		log.Error("bridge failed", "err", err)
		os.Exit(1)
	}
}

func run(log *slog.Logger) error {
	s, err := settings.Load(log)
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

	log.Info("joining the Stage as the bot", "ivsSDK", ivsrealtime.Version(), "model", s.ModelID, "region", awsCfg.Region)
	b, err := bot.Start(bot.Config{
		BotToken:     s.BotToken,
		Bedrock:      bedrockruntime.NewFromConfig(awsCfg),
		ModelID:      s.ModelID,
		SystemPrompt: s.SystemPrompt,
		VoiceID:      s.VoiceID,
		SessionLimit: s.SessionLimit,
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
