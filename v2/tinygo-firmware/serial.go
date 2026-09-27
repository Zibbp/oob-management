package main

import (
	"errors"
	"machine"
	"sync"
	"time"
)

const (
	serialMaxLines = 120
	serialMaxChars = 200
)

var (
	serialMu   sync.Mutex
	serialLens []string
	serialLine []byte
	serialUART = machine.UART0
)

// serialInit opens UART0 to the external device and starts the RX reader.
// RX/TX pins and baud come from config.go.
func serialInit() error {
	if err := serialUART.Configure(machine.UARTConfig{
		BaudRate: SERIAL_BAUD,
		TX:       machine.Pin(SERIAL_TX),
		RX:       machine.Pin(SERIAL_RX),
	}); err != nil {
		return err
	}
	go serialReader()
	logAdd("serial console: UART0 TX=%d RX=%d %d baud", SERIAL_TX, SERIAL_RX, SERIAL_BAUD)
	return nil
}

func serialPush(line string) {
	serialMu.Lock()
	serialLens = append(serialLens, line)
	if len(serialLens) > serialMaxLines {
		serialLens = serialLens[len(serialLens)-serialMaxLines:]
	}
	serialMu.Unlock()
}

func serialReader() {
	for {
		if serialUART.Buffered() == 0 {
			time.Sleep(5 * time.Millisecond)
			continue
		}
		b, err := serialUART.ReadByte()
		if err != nil {
			continue
		}
		switch b {
		case '\n':
			serialPush(string(serialLine))
			serialLine = serialLine[:0]
		case '\r':
			if len(serialLine) > 0 {
				serialPush(string(serialLine))
				serialLine = serialLine[:0]
			}
		case 0:
			// drop NULs
		default:
			if len(serialLine) < serialMaxChars {
				serialLine = append(serialLine, b)
			}
		}
	}
}

// serialText returns buffered device output as plain text.
func serialText() string {
	serialMu.Lock()
	defer serialMu.Unlock()
	out := ""
	for _, l := range serialLens {
		out += l + "\n"
	}
	return out
}

func serialClear() {
	serialMu.Lock()
	serialLens = nil
	serialLine = serialLine[:0]
	serialMu.Unlock()
}

// serialSend writes raw text to the external device.
func serialSend(s string) error {
	if s == "" {
		return errors.New("empty")
	}
	_, err := serialUART.Write([]byte(s))
	return err
}
