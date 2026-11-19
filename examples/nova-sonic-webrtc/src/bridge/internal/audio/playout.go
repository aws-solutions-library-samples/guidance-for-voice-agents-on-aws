package audio

import (
	"sync"
	"time"
)

// Sink receives paced mono S16 PCM. *ivsrealtime.AudioSource satisfies it.
type Sink interface {
	PushBuffer(data []byte, numFrames int, pts int64) error
}

// PlayoutStats are cumulative counters for a Playout.
type PlayoutStats struct {
	PushedFrames   int64         // frames pushed to the sink, audio and silence
	SilentFrames   int64         // frames pushed with no audio buffered (underrun)
	DroppedSamples int64         // samples rejected because the buffer was full
	FlushedSamples int64         // samples discarded by Flush (barge-in)
	PushErrors     int64         // PushBuffer calls that returned an error
	Buffered       time.Duration // audio waiting to play right now
}

// Playout is a jitter buffer that feeds a Sink in real time. Nova Sonic sends
// speech in bursts faster than real time, but AudioSource.PushBuffer drops
// audio pushed ahead of the clock, so Playout releases one 20 ms frame per 20 ms
// of wall time and pushes silence when it has nothing to play.
//
// Write, Flush, and Stats are safe from any goroutine.
type Playout struct {
	sink        Sink
	rate        int
	frameSize   int
	maxBuffered int
	maxLag      int64

	mu    sync.Mutex
	buf   []int16
	head  int
	stats PlayoutStats

	// Owned by the pacing goroutine.
	frame  []int16
	out    []byte
	pushed int64 // frames pushed so far, which is also the next pts

	started  bool
	start    time.Time
	stop     chan struct{}
	done     chan struct{}
	stopOnce sync.Once
}

// NewPlayout returns a Playout for mono audio at sampleRate Hz. It buffers at
// most two minutes of audio.
func NewPlayout(sink Sink, sampleRate int) *Playout {
	frameSize := sampleRate / 50
	return &Playout{
		sink:        sink,
		rate:        sampleRate,
		frameSize:   frameSize,
		maxBuffered: sampleRate * 120,
		maxLag:      int64(sampleRate / 5),
		frame:       make([]int16, frameSize),
		stop:        make(chan struct{}),
		done:        make(chan struct{}),
	}
}

// Write queues little-endian S16 mono PCM for playout.
func (p *Playout) Write(pcm []byte) {
	p.mu.Lock()
	defer p.mu.Unlock()
	room := p.maxBuffered - (len(p.buf) - p.head)
	if n := len(pcm) / 2; n > room {
		p.stats.DroppedSamples += int64(n - room)
		pcm = pcm[:room*2]
	}
	p.buf = BytesToS16(p.buf, pcm)
}

// Flush discards everything queued and returns how many samples it discarded.
// The bridge calls it when the user interrupts the assistant.
func (p *Playout) Flush() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	n := len(p.buf) - p.head
	p.buf, p.head = p.buf[:0], 0
	p.stats.FlushedSamples += int64(n)
	return n
}

// Stats returns a snapshot of the counters.
func (p *Playout) Stats() PlayoutStats {
	p.mu.Lock()
	defer p.mu.Unlock()
	s := p.stats
	s.Buffered = time.Duration(len(p.buf)-p.head) * time.Second / time.Duration(p.rate)
	return s
}

// Start begins pacing on its own goroutine.
func (p *Playout) Start() {
	p.started = true
	p.start = time.Now()
	go p.run()
}

// Stop ends pacing and waits for the goroutine to exit. No PushBuffer call is in
// flight once it returns. It is safe to call more than once.
func (p *Playout) Stop() {
	if !p.started {
		return
	}
	p.stopOnce.Do(func() { close(p.stop) })
	<-p.done
}

func (p *Playout) run() {
	defer close(p.done)
	ticker := time.NewTicker(5 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-p.stop:
			return
		case now := <-ticker.C:
			p.advance(now.Sub(p.start))
		}
	}
}

// advance pushes every frame due by elapsed wall time since Start. Pacing from
// the start time rather than counting ticks keeps timer jitter from drifting.
// If the goroutine falls far behind it skips ahead; the pts gap plays as silence.
func (p *Playout) advance(elapsed time.Duration) {
	due := int64(elapsed.Seconds() * float64(p.rate))
	if due-p.pushed > p.maxLag {
		p.pushed = due - int64(p.frameSize)
	}
	for p.pushed+int64(p.frameSize) <= due {
		p.pushFrame()
	}
}

func (p *Playout) pushFrame() {
	p.mu.Lock()
	n := copy(p.frame, p.buf[p.head:])
	clear(p.frame[n:])
	p.head += n
	switch {
	case p.head == len(p.buf):
		p.buf, p.head = p.buf[:0], 0
	case p.head > 1<<16 && p.head > len(p.buf)/2:
		k := copy(p.buf, p.buf[p.head:])
		p.buf, p.head = p.buf[:k], 0
	}
	p.stats.PushedFrames += int64(p.frameSize)
	if n == 0 {
		p.stats.SilentFrames += int64(p.frameSize)
	}
	p.mu.Unlock()

	p.out = S16ToBytes(p.out[:0], p.frame)
	if err := p.sink.PushBuffer(p.out, p.frameSize, p.pushed); err != nil {
		p.mu.Lock()
		p.stats.PushErrors++
		p.mu.Unlock()
	}
	p.pushed += int64(p.frameSize)
}
