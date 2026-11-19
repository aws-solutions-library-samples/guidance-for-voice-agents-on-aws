// Package nova speaks Amazon Nova Sonic's event protocol over Bedrock's
// InvokeModelWithBidirectionalStream, so the bridge streams audio to the model
// directly.
package nova

import (
	"encoding/base64"
	"encoding/json"
	"strings"
)

// SampleRate is the PCM rate in both directions: 16-bit mono LPCM at 24 kHz.
// Nova Sonic accepts 8, 16, or 24 kHz input and sends 24 kHz output.
const SampleRate = 24000

type object = map[string]any

// audioInputFormat describes the user's audio that the bridge sends.
func audioInputFormat() object {
	return object{
		"mediaType":       "audio/lpcm",
		"sampleRateHertz": SampleRate,
		"sampleSizeBits":  16,
		"channelCount":    1,
		"encoding":        "base64",
		"audioType":       "SPEECH",
	}
}

// audioOutputFormat describes the assistant's audio that Nova Sonic sends. It
// is separate from audioInputFormat so the two can differ.
func audioOutputFormat(voiceID string) object {
	return object{
		"mediaType":       "audio/lpcm",
		"sampleRateHertz": SampleRate,
		"sampleSizeBits":  16,
		"channelCount":    1,
		"voiceId":         voiceID,
		"encoding":        "base64",
		"audioType":       "SPEECH",
	}
}

func event(name string, body object) object {
	return object{"event": object{name: body}}
}

func sessionStart() object {
	return event("sessionStart", object{
		"inferenceConfiguration": object{"maxTokens": 1024, "topP": 0.9, "temperature": 0.7},
		// Inside sessionStart, not beside it.
		"turnDetectionConfiguration": object{"endpointingSensitivity": "MEDIUM"},
	})
}

func promptStart(prompt, voiceID string) object {
	return event("promptStart", object{
		"promptName":               prompt,
		"textOutputConfiguration":  object{"mediaType": "text/plain"},
		"audioOutputConfiguration": audioOutputFormat(voiceID),
	})
}

func systemContentStart(prompt, content string) object {
	return event("contentStart", object{
		"promptName":             prompt,
		"contentName":            content,
		"type":                   "TEXT",
		"interactive":            false,
		"role":                   "SYSTEM",
		"textInputConfiguration": object{"mediaType": "text/plain"},
	})
}

func textInput(prompt, content, text string) object {
	return event("textInput", object{"promptName": prompt, "contentName": content, "content": text})
}

func audioContentStart(prompt, content string) object {
	return event("contentStart", object{
		"promptName":              prompt,
		"contentName":             content,
		"type":                    "AUDIO",
		"interactive":             true,
		"role":                    "USER",
		"audioInputConfiguration": audioInputFormat(),
	})
}

func audioInput(prompt, content string, pcm []byte) object {
	return event("audioInput", object{
		"promptName":  prompt,
		"contentName": content,
		"content":     base64.StdEncoding.EncodeToString(pcm),
	})
}

func contentEnd(prompt, content string) object {
	return event("contentEnd", object{"promptName": prompt, "contentName": content})
}

func promptEnd(prompt string) object {
	return event("promptEnd", object{"promptName": prompt})
}

func sessionEnd() object {
	return event("sessionEnd", object{})
}

// output is one thing the model said, decoded from an output event.
type output struct {
	kind  outputKind
	pcm   []byte // audio
	role  string // transcript: USER or ASSISTANT
	text  string // transcript
	final bool   // transcript: false for speculative assistant text
}

type outputKind int

const (
	outputAudio outputKind = iota + 1
	outputTranscript
	outputInterrupt
)

// parser turns Nova Sonic output events into outputs. Text arrives in content
// blocks: contentStart carries the role and, for the assistant, whether the
// text is SPECULATIVE (about to be spoken) or FINAL (actually spoken), so the
// parser tracks blocks by contentId to label each textOutput.
type parser struct {
	blocks map[string]block
}

type block struct {
	role  string
	stage string
}

type envelope struct {
	Event struct {
		ContentStart *struct {
			ContentID             string `json:"contentId"`
			Role                  string `json:"role"`
			AdditionalModelFields string `json:"additionalModelFields"`
		} `json:"contentStart"`
		TextOutput *struct {
			ContentID string `json:"contentId"`
			Role      string `json:"role"`
			Content   string `json:"content"`
		} `json:"textOutput"`
		AudioOutput *struct {
			Content string `json:"content"`
		} `json:"audioOutput"`
		ContentEnd *struct {
			ContentID  string `json:"contentId"`
			StopReason string `json:"stopReason"`
		} `json:"contentEnd"`
	} `json:"event"`
}

func newParser() *parser { return &parser{blocks: map[string]block{}} }

// parse decodes one output event. Events the bridge doesn't use, such as
// usageEvent or completionStart, yield nothing.
func (p *parser) parse(raw []byte) ([]output, error) {
	var env envelope
	if err := json.Unmarshal(raw, &env); err != nil {
		return nil, err
	}
	ev := env.Event
	switch {
	case ev.AudioOutput != nil:
		pcm, err := base64.StdEncoding.DecodeString(ev.AudioOutput.Content)
		if err != nil {
			return nil, err
		}
		return []output{{kind: outputAudio, pcm: pcm}}, nil

	case ev.ContentStart != nil:
		stage := "FINAL"
		if f := ev.ContentStart.AdditionalModelFields; f != "" {
			var fields struct {
				GenerationStage string `json:"generationStage"`
			}
			if json.Unmarshal([]byte(f), &fields) == nil && fields.GenerationStage != "" {
				stage = fields.GenerationStage
			}
		}
		p.blocks[ev.ContentStart.ContentID] = block{role: ev.ContentStart.Role, stage: stage}
		return nil, nil

	case ev.TextOutput != nil:
		if isInterruption(ev.TextOutput.Content) {
			return []output{{kind: outputInterrupt}}, nil
		}
		b, ok := p.blocks[ev.TextOutput.ContentID]
		if !ok {
			b = block{stage: "FINAL"}
		}
		role := ev.TextOutput.Role
		if role == "" {
			role = b.role
		}
		if role == "" {
			role = "ASSISTANT"
		}
		return []output{{kind: outputTranscript, role: role, text: ev.TextOutput.Content, final: b.stage != "SPECULATIVE"}}, nil

	case ev.ContentEnd != nil:
		delete(p.blocks, ev.ContentEnd.ContentID)
		if ev.ContentEnd.StopReason == "INTERRUPTED" {
			return []output{{kind: outputInterrupt}}, nil
		}
	}
	return nil, nil
}

// isInterruption reports whether assistant text is Nova Sonic's barge-in
// signal, {"interrupted": true}.
func isInterruption(text string) bool {
	text = strings.TrimSpace(text)
	if !strings.HasPrefix(text, "{") {
		return false
	}
	var v struct {
		Interrupted bool `json:"interrupted"`
	}
	return json.Unmarshal([]byte(text), &v) == nil && v.Interrupted
}
