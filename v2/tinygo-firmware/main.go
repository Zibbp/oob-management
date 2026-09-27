package main

import (
	"machine"
	"time"
)

func main() {
	println(AppName, AppVersion, "boot")
	led := machine.Pin(LEDPin)
	led.Configure(machine.PinConfig{Mode: machine.PinOutput})
	led.High()
	// On-board LED: slow blink until network is up, solid after.
	led2 := machine.LED
	led2.Configure(machine.PinConfig{Mode: machine.PinOutput})

	storeLoad()
	if err := mcpInit(); err != nil {
		logAdd("MCP init failed: %s", err)
	}
	if err := kvmInit(); err != nil {
		logAdd("KVM init failed: %s", err)
	}
	if err := serialInit(); err != nil {
		logAdd("serial init failed: %s", err)
	}

	for {
		if err := netBringUp(); err != nil {
			logAdd("net failed: %s; retry", err)
			for i := 0; i < 6; i++ {
				led.Low()
				led2.High()
				time.Sleep(250 * time.Millisecond)
				led.High()
				led2.Low()
				time.Sleep(250 * time.Millisecond)
			}
			continue
		}
		break
	}
	led.High()
	led2.High()
	serveForever(routes())
}
