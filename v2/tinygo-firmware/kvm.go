package main

import (
	"errors"
	"machine"

	pio "github.com/tinygo-org/pio/rp2-pio"
)

var kvmSM pio.StateMachine

// kvmInit drives the KVM switch UART with PIO, replicating old/main.py's
// pio_uart_tx: SM clock 8x baud, start bit 8 cycles, 8 data bits LSB-first,
// stop 7 cycles. Sideset is OPTIONAL (like the Python version): only explicit
// .side() touches the pin, so the line idles HIGH and every start bit has a
// real falling edge.
func kvmInit() error {
	pin := machine.Pin(KVMTXPin)
	sm, err := pio.PIO0.ClaimStateMachine()
	if err != nil {
		return err
	}

	// SidesetBits=2 covers EN+DATA. PINCTRL count INCLUDES the option bit
	// (MicroPython programs 2 for `.side_set 1 opt`); 1 would leave no
	// data bit and pin the line.
	asm := pio.AssemblerV0{SidesetBits: 2}
	const bitloop = 2
	program := []uint16{
		asm.Pull(false, true).Encode(),                        // 0: pull block
		asm.Set(pio.SetDestX, 7).Side(0b10).Delay(7).Encode(), // 1: start bit
		asm.Out(pio.OutDestPins, 1).Delay(6).Encode(),         // 2: data bit
		asm.Jmp(pio.JmpXNZeroDec, bitloop).Encode(),           // 3: loop 8 bits
		asm.Nop().Side(0b11).Delay(6).Encode(),                // 4: stop bit
	}

	dev := sm.PIO()
	offset, err := dev.AddProgram(program, -1)
	if err != nil {
		return err
	}
	pin.Configure(machine.PinConfig{Mode: dev.PinMode()})
	pin.High() // idle high, like sideset_init/out_init=OUT_HIGH
	sm.SetPindirsConsecutive(pin, 1, true)

	cfg := asm.DefaultStateMachineConfig(offset, program)
	cfg.SetSidesetParams(2, true, false)
	cfg.SetSidesetPins(pin)
	cfg.SetOutPins(pin, 1)
	cfg.SetInShift(false, false, 32)
	cfg.SetOutShift(true, false, 32)
	whole, frac, err := pio.ClkDivFromFrequency(8*KVMBaud, machine.CPUFrequency())
	if err != nil {
		return err
	}
	cfg.SetClkDivIntFrac(whole, frac)
	sm.Init(offset, cfg)
	sm.SetEnabled(true)

	kvmSM = sm
	logAdd("KVM PIO ready GP%d div=%d.%d cpu=%d enabled=%v",
		KVMTXPin, whole, frac, machine.CPUFrequency(), sm.IsEnabled())
	return nil
}

func kvmSwitch(port int) error {
	// Sends e.g. "G01gA", same bytes as the Python version.
	//
	// The command is queued with no allocations, sleeps, or yields: under
	// TinyGo's cooperative scheduler that makes the puts atomic, so the PIO
	// FIFO never empties mid-command.
	if port < 1 || port > len(PCS) {
		return errors.New("invalid KVM port")
	}
	if !kvmSM.IsValid() {
		return errors.New("KVM UART is not available")
	}
	cmd := [5]byte{'G', '0', byte('0' + port), 'g', 'A'}
	for i := 0; i < len(cmd); i++ {
		for kvmSM.IsTxFIFOFull() {
		}
		kvmSM.TxPut(uint32(cmd[i]))
	}
	logAdd("KVM switched to port %d", port)
	return nil
}
