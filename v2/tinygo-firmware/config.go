package main

// Hardware wiring (matches old MicroPython setup) and app constants.

const (
	AppName    = "OOB Management"
	AppVersion = "3.0.0"
	Hostname   = "oob-pico"
	HTTPPort   = ":80"

	// W5500 on SPI0.
	W5500SCK  = 18
	W5500MOSI = 19
	W5500MISO = 16
	W5500CS   = 17
	W5500RST  = 20

	// MCP23017 on I2C1.
	MCPSDA   = 2
	MCPSCL   = 3
	MCPAddr  = 0x20
	MCPFreq  = 100_000
	MCPBankA = 0
	MCPBankB = 1

	// KVM transmit-only serial + controller LED.
	KVMTXPin = 5
	KVMBaud  = 19200
	LEDPin   = 6

	// External device serial console on UART0.
	SERIAL_TX   = 0
	SERIAL_RX   = 1
	SERIAL_BAUD = 115200

	// Action pulse lengths.
	PowerPulse = 1000 // ms
	ResetPulse = 700  // ms
	ForcePulse = 5000 // ms

	// Default credentials (overridable via web UI, persisted in flash).
	DefaultUser = "oob"
	DefaultPass = "oob"
)

// PC maps one ATX header group to MCP23017 pins.
type PC struct {
	ID    int
	Name  string
	Bank  uint8 // MCPBankA or MCPBankB
	Power uint8 // output: power switch pulse
	Reset uint8 // output: reset switch pulse
	Led   uint8 // input: motherboard power LED sense
	Aux   uint8 // input: spare/aux sense
}

// Pin converts a bank+bit to MCP23017 driver pin number (0-7=A, 8-15=B).
func (p PC) Pin(bit uint8) int {
	if p.Bank == MCPBankB {
		return int(bit) + 8
	}
	return int(bit)
}

// PCS mirrors old/main.py. Adjust if the harness order changes.
var PCS = []PC{
	{ID: 1, Name: "PC 1", Bank: MCPBankA, Power: 0, Reset: 1, Led: 2, Aux: 3},
	{ID: 2, Name: "PC 2", Bank: MCPBankA, Power: 4, Reset: 5, Led: 6, Aux: 7},
	{ID: 3, Name: "PC 3", Bank: MCPBankB, Power: 0, Reset: 1, Led: 2, Aux: 3},
	{ID: 4, Name: "PC 4", Bank: MCPBankB, Power: 4, Reset: 5, Led: 6, Aux: 7},
}

func findPC(id int) *PC {
	for i := range PCS {
		if PCS[i].ID == id {
			return &PCS[i]
		}
	}
	return nil
}
