package main

import (
	"fmt"
	"sync"
	"time"
)

const (
	logMaxEntries = 80
	logMaxChars   = 160
)

var (
	logMu    sync.Mutex
	logLines []string
)

// logAdd appends a timestamped line to the in-RAM ring.
func logAdd(format string, args ...any) {
	msg := fmt.Sprintf(format, args...)
	if len(msg) > logMaxChars {
		msg = msg[:logMaxChars-3] + "..."
	}
	line := fmt.Sprintf("[%d] %s", time.Now().UnixMilli(), msg)
	println(line)
	logMu.Lock()
	logLines = append(logLines, line)
	if len(logLines) > logMaxEntries {
		logLines = logLines[len(logLines)-logMaxEntries:]
	}
	logMu.Unlock()
}

// logText returns the ring as plain text.
func logText() string {
	logMu.Lock()
	defer logMu.Unlock()
	out := ""
	for _, l := range logLines {
		out += l + "\n"
	}
	return out
}
