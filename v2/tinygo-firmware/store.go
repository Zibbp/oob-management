package main

import (
	"machine"
	"sync"
)

const (
	storeMagic0 = 0x4F // 'O'
	storeMagic1 = 0x42 // 'B'
	storeVer    = 1

	labelLen = 32
	userLen  = 32
	passLen  = 64
)

// store layout in one 4K flash sector: magic ver labels user pass checksum.
const storeSize = 2 + 1 + 4*labelLen + userLen + passLen + 1

var (
	storeMu   sync.Mutex
	labels    [4]string
	authUser  = DefaultUser
	authPass  = DefaultPass
	storeOff  int64 // offset of persist sector from FlashDataStart
	storeInit bool
)

func storeSector() (off int64, size int64) {
	size = machine.Flash.Size()
	return size - machine.Flash.EraseBlockSize(), machine.Flash.EraseBlockSize()
}

func checksum(b []byte) byte {
	var s byte
	for _, v := range b {
		s += v
	}
	return s
}

func fixedStr(s string, n int) []byte {
	b := make([]byte, n)
	copy(b, s)
	return b
}

// storeLoad reads labels + auth from flash, or defaults.
func storeLoad() {
	for i, pc := range PCS {
		labels[i] = pc.Name
	}
	off, _ := storeSector()
	storeOff = off
	buf := make([]byte, storeSize)
	if _, err := machine.Flash.ReadAt(buf, off); err != nil {
		logAdd("store: read err, using defaults")
		return
	}
	if buf[0] != storeMagic0 || buf[1] != storeMagic1 || buf[2] != storeVer {
		logAdd("store: empty, using defaults")
		return
	}
	if checksum(buf[:storeSize-1]) != buf[storeSize-1] {
		logAdd("store: bad checksum, using defaults")
		return
	}
	p := 3
	for i := range labels {
		labels[i] = cStr(buf[p : p+labelLen])
		if labels[i] == "" {
			labels[i] = PCS[i].Name
		}
		p += labelLen
	}
	if u := cStr(buf[p : p+userLen]); u != "" {
		authUser = u
	}
	p += userLen
	if pw := cStr(buf[p : p+passLen]); pw != "" {
		authPass = pw
	}
	storeInit = true
	logAdd("store: loaded")
}

func cStr(b []byte) string {
	for i, v := range b {
		if v == 0 {
			return string(b[:i])
		}
	}
	return string(b)
}

// storeSave persists current labels + auth to flash.
func storeSave() error {
	storeMu.Lock()
	defer storeMu.Unlock()
	buf := make([]byte, 0, storeSize)
	buf = append(buf, storeMagic0, storeMagic1, storeVer)
	for _, l := range labels {
		buf = append(buf, fixedStr(l, labelLen)...)
	}
	buf = append(buf, fixedStr(authUser, userLen)...)
	buf = append(buf, fixedStr(authPass, passLen)...)
	buf = append(buf, checksum(buf))
	blk := storeOff / machine.Flash.EraseBlockSize()
	if err := machine.Flash.EraseBlocks(blk, 1); err != nil {
		return err
	}
	_, err := machine.Flash.WriteAt(buf, storeOff)
	return err
}

func labelFor(id int) string {
	for i, pc := range PCS {
		if pc.ID == id {
			return labels[i]
		}
	}
	return ""
}

func setLabel(id int, name string) bool {
	for i, pc := range PCS {
		if pc.ID == id {
			labels[i] = name
			return true
		}
	}
	return false
}
