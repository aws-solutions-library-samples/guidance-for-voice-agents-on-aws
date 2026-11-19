package settings

import (
	"bytes"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// tokenFile writes a bot token file with the given mode and points
// BOT_TOKEN_FILE at it.
func tokenFile(t *testing.T, mode os.FileMode) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "bot-token")
	if err := os.WriteFile(path, []byte("bot-token-value\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, mode); err != nil { // not subject to the umask
		t.Fatal(err)
	}
	t.Setenv("BOT_TOKEN_FILE", path)
}

func load(t *testing.T) (Settings, string, error) {
	t.Helper()
	var logs bytes.Buffer
	s, err := Load(slog.New(slog.NewTextHandler(&logs, nil)))
	return s, logs.String(), err
}

func TestDefaults(t *testing.T) {
	for _, k := range []string{"NOVA_MODEL_ID", "SYSTEM_PROMPT", "VOICE_ID", "SESSION_LIMIT_SECONDS"} {
		t.Setenv(k, "")
	}
	tokenFile(t, 0o600)
	s, _, err := load(t)
	if err != nil {
		t.Fatal(err)
	}
	want := Settings{
		BotToken:     "bot-token-value",
		ModelID:      "amazon.nova-2-5-sonic",
		SystemPrompt: DefaultSystemPrompt,
		VoiceID:      "matthew",
		SessionLimit: 465 * time.Second,
	}
	if s != want {
		t.Errorf("got %+v, want %+v", s, want)
	}
}

func TestSessionLimit(t *testing.T) {
	tokenFile(t, 0o600)
	const notPositive = "SESSION_LIMIT_SECONDS must be a positive number of seconds"
	for _, c := range []struct {
		value   string
		want    time.Duration
		wantErr string
	}{
		{"465", 465 * time.Second, ""},
		{"479", 479 * time.Second, ""},
		{"480", 0, "SESSION_LIMIT_SECONDS is 480 but must be less than 480, Bedrock's 8-minute stream limit"},
		{"600", 0, "SESSION_LIMIT_SECONDS is 600 but must be less than 480, Bedrock's 8-minute stream limit"},
		{"0", 0, notPositive},
		{"-1", 0, notPositive},
		{"x", 0, notPositive},
	} {
		t.Setenv("SESSION_LIMIT_SECONDS", c.value)
		s, _, err := load(t)
		switch {
		case c.wantErr == "" && err != nil:
			t.Errorf("SESSION_LIMIT_SECONDS=%s: %v", c.value, err)
		case c.wantErr == "" && s.SessionLimit != c.want:
			t.Errorf("SESSION_LIMIT_SECONDS=%s: limit %s, want %s", c.value, s.SessionLimit, c.want)
		case c.wantErr != "" && (err == nil || err.Error() != c.wantErr):
			t.Errorf("SESSION_LIMIT_SECONDS=%s: error %v, want %q", c.value, err, c.wantErr)
		}
	}
}

func TestTokenFileModeWarning(t *testing.T) {
	t.Setenv("SESSION_LIMIT_SECONDS", "")
	for _, c := range []struct {
		mode os.FileMode
		warn bool
	}{
		{0o600, false},
		{0o400, false},
		{0o644, true},
		{0o640, true},
		{0o604, true},
	} {
		tokenFile(t, c.mode)
		s, logs, err := load(t)
		if err != nil || s.BotToken != "bot-token-value" {
			t.Errorf("mode %o: token %q, error %v", c.mode, s.BotToken, err)
		}
		warned := strings.Contains(logs, "level=WARN") && strings.Contains(logs, "BOT_TOKEN_FILE is accessible to other users")
		if warned != c.warn {
			t.Errorf("mode %o: warned %v, want %v; logs:\n%s", c.mode, warned, c.warn, logs)
		}
	}
}

func TestBotTokenFromEnvironment(t *testing.T) {
	t.Setenv("SESSION_LIMIT_SECONDS", "")
	t.Setenv("BOT_TOKEN_FILE", "")
	t.Setenv("BOT_TOKEN", " env-token ")
	s, _, err := load(t)
	if err != nil || s.BotToken != "env-token" {
		t.Errorf("token %q, error %v", s.BotToken, err)
	}
	if _, ok := os.LookupEnv("BOT_TOKEN"); ok {
		t.Error("BOT_TOKEN still in the environment")
	}

	t.Setenv("BOT_TOKEN", "")
	if _, _, err := load(t); err == nil {
		t.Error("no token accepted")
	}
}
