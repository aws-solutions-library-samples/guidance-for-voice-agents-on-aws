// Package bot is the Stage participant that connects users to Nova Sonic.
// It joins the Stage once at startup with the bot token, then holds one
// conversation at a time: it subscribes to one user's audio, streams it to Nova
// Sonic over Bedrock, and publishes Nova's speech back to the Stage.
package bot

import (
	"sync"

	ivsrealtime "github.com/aws/amazon-ivs-realtime-go"
)

// strategy subscribes to audio from at most one participant, the user the bot
// is currently talking with, and always publishes the bot's audio source.
//
// The SDK calls these methods on its event thread; the conversation controller
// changes the active user from its own goroutine, hence the mutex.
type strategy struct {
	source *ivsrealtime.AudioSource
	// onJoined receives the bot's own participant info. The SDK asks
	// ShouldPublish about the local participant once it has joined, and only
	// if its token can publish, so this is the first point that info exists.
	onJoined func(ivsrealtime.ParticipantInfo)
	// onSeen receives every remote participant the SDK asks about.
	onSeen func(ivsrealtime.ParticipantInfo)
	once   sync.Once

	mu     sync.Mutex
	active string
}

func (s *strategy) setActive(participantID string) {
	s.mu.Lock()
	s.active = participantID
	s.mu.Unlock()
}

func (s *strategy) isActive(participantID string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return participantID != "" && participantID == s.active
}

func (s *strategy) SubscriptionTypes(p ivsrealtime.ParticipantInfo) ivsrealtime.SubscribeType {
	s.onSeen(p)
	if s.isActive(p.ParticipantID) {
		return ivsrealtime.SubscribeTypeAudio
	}
	return ivsrealtime.SubscribeTypeNone
}

func (s *strategy) ShouldPublish(p ivsrealtime.ParticipantInfo) bool {
	s.once.Do(func() { s.onJoined(p) })
	return true
}

func (s *strategy) StreamsToPublish(ivsrealtime.ParticipantInfo) ivsrealtime.PublishStreams {
	return ivsrealtime.PublishStreams{Audio: s.source}
}
