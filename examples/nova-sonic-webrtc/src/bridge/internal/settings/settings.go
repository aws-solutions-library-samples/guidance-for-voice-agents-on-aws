// Package settings reads the bridge's settings from environment variables. It
// doesn't depend on the IVS SDK, so its tests run anywhere Go does.
package settings

import (
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"
)

// bedrockStreamLimit is Bedrock's 8-minute limit on one bidirectional stream,
// in seconds. SESSION_LIMIT_SECONDS must be shorter so the bridge ends each
// session cleanly first.
const bedrockStreamLimit = 480

// DefaultSystemPrompt is the system prompt used when SYSTEM_PROMPT is unset.
const DefaultSystemPrompt = "You are a friendly assistant talking with the user over a live audio call. " +
	"Keep your answers short and conversational, usually two or three sentences."

// Settings are the bridge's settings, read from environment variables.
type Settings struct {
	BotToken     string
	ModelID      string
	SystemPrompt string
	VoiceID      string
	SessionLimit time.Duration
}

// Load reads the settings from the environment. It warns through log if other
// users can read BOT_TOKEN_FILE.
func Load(log *slog.Logger) (Settings, error) {
	s := Settings{
		ModelID:      envOr("NOVA_MODEL_ID", "amazon.nova-2-5-sonic"),
		SystemPrompt: envOr("SYSTEM_PROMPT", DefaultSystemPrompt),
		VoiceID:      envOr("VOICE_ID", "matthew"),
	}
	limit, err := strconv.Atoi(envOr("SESSION_LIMIT_SECONDS", "465"))
	if err != nil || limit <= 0 {
		return s, fmt.Errorf("SESSION_LIMIT_SECONDS must be a positive number of seconds")
	}
	if limit >= bedrockStreamLimit {
		return s, fmt.Errorf("SESSION_LIMIT_SECONDS is %d but must be less than %d, Bedrock's 8-minute stream limit", limit, bedrockStreamLimit)
	}
	s.SessionLimit = time.Duration(limit) * time.Second
	token, err := botToken(log)
	if err != nil {
		return s, err
	}
	s.BotToken = token
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

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
