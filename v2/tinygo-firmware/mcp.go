package main

import (
	"errors"
	"machine"
	"time"

	"tinygo.org/x/drivers/mcp23017"
)

var mcpDev *mcp23017.Device

// mcpInit brings up I2C1 and configures power/reset as outputs,
// LED/aux as inputs with pull-ups and inverted (active-low) sense.
func mcpInit() error {
	if err := machine.I2C1.Configure(machine.I2CConfig{
		Frequency: MCPFreq,
		SDA:       MCPSDA,
		SCL:       MCPSCL,
	}); err != nil {
		return err
	}
	dev, err := mcp23017.NewI2C(machine.I2C1, MCPAddr)
	if err != nil {
		return err
	}
	modes := make([]mcp23017.PinMode, mcp23017.PinCount)
	for i := range modes {
		modes[i] = mcp23017.Input | mcp23017.Pullup | mcp23017.Invert
	}
	for _, pc := range PCS {
		modes[pc.Pin(pc.Power)] = mcp23017.Output
		modes[pc.Pin(pc.Reset)] = mcp23017.Output
	}
	if err := dev.SetModes(modes); err != nil {
		return err
	}
	// Outputs idle low (switch released).
	var pins, mask mcp23017.Pins
	for _, pc := range PCS {
		mask.High(pc.Pin(pc.Power))
		mask.High(pc.Pin(pc.Reset))
	}
	if err := dev.SetPins(pins, mask); err != nil {
		return err
	}
	mcpDev = dev
	return nil
}

func mcpRead(bank uint8, bit uint8) bool {
	pc := PC{Bank: bank}
	v, err := mcpDev.Pin(pc.Pin(bit)).Get()
	if err != nil {
		return false
	}
	return v
}

func mcpPulse(bank uint8, bit uint8, ms int) {
	pc := PC{Bank: bank}
	p := mcpDev.Pin(pc.Pin(bit))
	_ = p.High()
	time.Sleep(time.Duration(ms) * time.Millisecond)
	_ = p.Low()
}

// runAction pulses the ATX switch. Mirrors old run_action.
func runAction(id int, action string) error {
	pc := findPC(id)
	if pc == nil {
		return errors.New("unknown PC")
	}
	switch action {
	case "power", "poweron":
		logAdd("power pulse PC %d", id)
		mcpPulse(pc.Bank, pc.Power, PowerPulse)
	case "reset":
		logAdd("reset pulse PC %d", id)
		mcpPulse(pc.Bank, pc.Reset, ResetPulse)
	case "force_off", "poweroff":
		logAdd("force-off PC %d", id)
		mcpPulse(pc.Bank, pc.Power, ForcePulse)
	default:
		return errors.New("unknown action")
	}
	return nil
}
