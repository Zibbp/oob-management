package main

import (
	"encoding/binary"
	"machine"
	"net"
	"net/netip"
	"time"

	"tinygo.org/x/drivers/netdev"
	"tinygo.org/x/drivers/w5500"
)

var (
	ethDev    *w5500.Device
	currentIP = "0.0.0.0"
	macAddr   net.HardwareAddr
)

const (
	dhcpServerPort = 67
	dhcpClientPort = 68
	dhcpTimeout    = 30 * time.Second
)

// makeMAC derives a stable locally-administered MAC from the flash ID.
func makeMAC() net.HardwareAddr {
	id := machine.DeviceID()
	m := net.HardwareAddr{0x02, 0x4F, 0x4F, 0x42, 0x00, 0x00}
	if len(id) >= 2 {
		m[4] = id[len(id)-2]
		m[5] = id[len(id)-1]
	}
	if m[4] == 0 && m[5] == 0 {
		m[4], m[5] = 0x12, 0x34
	}
	return m
}

// netBringUp resets the W5500, runs DHCP, and registers the netdev stack.
func netBringUp() error {
	macAddr = makeMAC()
	logAdd("MAC %s", macAddr.String())

	machine.SPI0.Configure(machine.SPIConfig{
		Frequency: 8_000_000,
		SCK:       machine.Pin(W5500SCK),
		SDO:       machine.Pin(W5500MOSI),
		SDI:       machine.Pin(W5500MISO),
	})
	cs := machine.Pin(W5500CS)
	cs.Configure(machine.PinConfig{Mode: machine.PinOutput})
	cs.High()
	rst := machine.Pin(W5500RST)
	rst.Configure(machine.PinConfig{Mode: machine.PinOutput})
	rst.Low()
	time.Sleep(100 * time.Millisecond)
	rst.High()
	time.Sleep(500 * time.Millisecond)

	ethDev = w5500.New(machine.SPI0, cs)
	zero := netip.AddrFrom4([4]byte{0, 0, 0, 0})
	if err := ethDev.Configure(w5500.Config{
		MAC:        macAddr,
		IP:         zero,
		SubnetMask: zero,
		Gateway:    zero,
		MaxSockets: 8, // 1 listener + headroom for parallel browser fetches
	}); err != nil {
		return err
	}
	// Sanity: talk to the chip. Unconfigured CS/wiring reads back wrong MAC.
	if got, err := ethDev.GetHardwareAddr(); err != nil || got.String() != macAddr.String() {
		return errTimeout("W5500 not responding")
	}

	// Wait for link.
	linkDeadline := time.Now().Add(dhcpTimeout)
	for ethDev.LinkStatus() != w5500.LinkStatusUp {
		if time.Now().After(linkDeadline) {
			return errTimeout("ethernet link down")
		}
		logAdd("waiting for link...")
		time.Sleep(time.Second)
	}
	logAdd("link up: %s", ethDev.LinkInfo())

	ip, mask, gw, err := dhcpLease()
	if err != nil {
		return err // no static fallback: caller retries with LED blink
	}
	if err := ethDev.SetAddr(ip); err != nil {
		return err
	}
	if err := ethDev.SetSubnetMask(mask); err != nil {
		return err
	}
	if err := ethDev.SetGateway(gw); err != nil {
		return err
	}
	currentIP = ip.String()
	logAdd("net: ip=%s mask=%s gw=%s", ip, mask, gw)
	netdev.UseNetdev(ethDev)
	return nil
}

type dhcpErr string

func (e dhcpErr) Error() string { return string(e) }
func errTimeout(s string) error { return dhcpErr(s) }

type lease struct {
	ip, mask, gw netip.Addr
}

func dhcpLease() (ip, mask, gw netip.Addr, err error) {
	xid := makeXID()
	deadline := time.Now().Add(dhcpTimeout)
	for time.Now().Before(deadline) {
		offer, oErr := dhcpDiscover(xid)
		if oErr != nil {
			logAdd("dhcp discover: %s", oErr)
			continue
		}
		l, rErr := dhcpRequest(xid, offer)
		if rErr != nil {
			logAdd("dhcp request: %s", rErr)
			continue
		}
		return l.ip, l.mask, l.gw, nil
	}
	return ip, mask, gw, errTimeout("DHCP timeout")
}

func makeXID() uint32 {
	id := machine.DeviceID()
	var x uint32 = 0x4F4F4200
	for _, b := range id {
		x = x*31 + uint32(b)
	}
	return x | 1
}

// dhcpPacket builds a minimal BOOTREQUEST. msgType 1=discover, 3=request.
func dhcpPacket(xid uint32, msgType byte, reqIP, srvIP netip.Addr) []byte {
	p := make([]byte, 300)
	p[0], p[1], p[2] = 1, 1, 6 // bootrequest, ethernet, mac len
	binary.BigEndian.PutUint32(p[4:8], xid)
	binary.BigEndian.PutUint16(p[10:12], 0x8000) // broadcast flag
	copy(p[28:34], macAddr)
	copy(p[236:240], []byte{99, 130, 83, 99})
	o := p[240:]
	put := func(code byte, data ...byte) {
		o[0], o[1] = code, byte(len(data))
		copy(o[2:], data)
		o = o[2+len(data):]
	}
	put(53, msgType)
	put(55, 1, 3, 6, 15)
	put(57, 0x02, 0x40)
	o[0], o[1] = 61, 7
	o[2] = 1
	copy(o[3:9], macAddr)
	o = o[9:]
	o[0], o[1] = 12, byte(len(Hostname))
	copy(o[2:], Hostname)
	o = o[2+len(Hostname):]
	if msgType == 3 {
		put(50, reqIP.AsSlice()...)
		put(54, srvIP.AsSlice()...)
	}
	o[0] = 255
	end := len(p) - len(o) + 1
	return p[:end]
}

func parseOpts(b []byte) map[byte][]byte {
	m := map[byte][]byte{}
	for i := 0; i < len(b); {
		c := b[i]
		if c == 255 {
			break
		}
		if c == 0 {
			i++
			continue
		}
		if i+1 >= len(b) {
			break
		}
		n := int(b[i+1])
		if i+2+n > len(b) {
			break
		}
		m[c] = b[i+2 : i+2+n]
		i += 2 + n
	}
	return m
}

// dhcpDiscover sends DISCOVER and returns offer details incl server IP.
func dhcpDiscover(xid uint32) (offer dhcpOffer, err error) {
	fd, err := ethDev.Socket(netdev.AF_INET, netdev.SOCK_DGRAM, netdev.IPPROTO_UDP)
	if err != nil {
		return offer, err
	}
	defer ethDev.Close(fd)
	if err := ethDev.Bind(fd, netip.AddrPortFrom(netip.AddrFrom4([4]byte{}), dhcpClientPort)); err != nil {
		return offer, err
	}
	bcast := netip.AddrPortFrom(netip.AddrFrom4([4]byte{255, 255, 255, 255}), dhcpServerPort)
	if err := ethDev.Connect(fd, "", bcast); err != nil {
		return offer, err
	}
	pkt := dhcpPacket(xid, 1, netip.Addr{}, netip.Addr{})
	if _, err := ethDev.Send(fd, pkt, 0, time.Now().Add(2*time.Second)); err != nil {
		return offer, err
	}
	buf := make([]byte, 1024)
	deadline := time.Now().Add(4 * time.Second)
	for time.Now().Before(deadline) {
		n, rerr := ethDev.Recv(fd, buf, 0, time.Now().Add(500*time.Millisecond))
		// W5500 UDP recv includes an 8-byte header: sender IP, port, length.
		if rerr != nil || n < 240+8 {
			continue
		}
		b := buf[8:n]
		if b[0] != 2 || binary.BigEndian.Uint32(b[4:8]) != xid {
			continue
		}
		opts := parseOpts(b[240:])
		if opts[53] == nil || opts[53][0] != 2 {
			continue
		}
		copy(offer.yiaddr[:], b[16:20])
		if v := opts[1]; len(v) == 4 {
			offer.mask = netip.AddrFrom4([4]byte{v[0], v[1], v[2], v[3]})
		} else {
			offer.mask = netip.AddrFrom4([4]byte{255, 255, 255, 0})
		}
		if v := opts[3]; len(v) >= 4 {
			offer.gw = netip.AddrFrom4([4]byte{v[0], v[1], v[2], v[3]})
		} else {
			offer.gw = netip.AddrFrom4(offer.yiaddr)
			offer.gw = netip.AddrFrom4([4]byte{offer.yiaddr[0], offer.yiaddr[1], offer.yiaddr[2], 1})
		}
		if v := opts[54]; len(v) == 4 {
			offer.server = netip.AddrFrom4([4]byte{v[0], v[1], v[2], v[3]})
		} else {
			offer.server = offer.gw
		}
		return offer, nil
	}
	return offer, errTimeout("no offer")
}

type dhcpOffer struct {
	yiaddr [4]byte
	mask   netip.Addr
	gw     netip.Addr
	server netip.Addr
}

func dhcpRequest(xid uint32, o dhcpOffer) (lease, error) {
	var l lease
	fd, err := ethDev.Socket(netdev.AF_INET, netdev.SOCK_DGRAM, netdev.IPPROTO_UDP)
	if err != nil {
		return l, err
	}
	defer ethDev.Close(fd)
	if err := ethDev.Bind(fd, netip.AddrPortFrom(netip.AddrFrom4([4]byte{}), dhcpClientPort)); err != nil {
		return l, err
	}
	bcast := netip.AddrPortFrom(netip.AddrFrom4([4]byte{255, 255, 255, 255}), dhcpServerPort)
	if err := ethDev.Connect(fd, "", bcast); err != nil {
		return l, err
	}
	pkt := dhcpPacket(xid, 3, netip.AddrFrom4(o.yiaddr), o.server)
	if _, err := ethDev.Send(fd, pkt, 0, time.Now().Add(2*time.Second)); err != nil {
		return l, err
	}
	buf := make([]byte, 1024)
	deadline := time.Now().Add(4 * time.Second)
	for time.Now().Before(deadline) {
		n, rerr := ethDev.Recv(fd, buf, 0, time.Now().Add(500*time.Millisecond))
		if rerr != nil || n < 240+8 {
			continue
		}
		b := buf[8:n]
		if b[0] != 2 || binary.BigEndian.Uint32(b[4:8]) != xid {
			continue
		}
		opts := parseOpts(b[240:])
		if opts[53] == nil || opts[53][0] != 5 {
			continue
		}
		var yi [4]byte
		copy(yi[:], b[16:20])
		l.ip = netip.AddrFrom4(yi)
		l.mask = o.mask
		l.gw = o.gw
		if v := opts[1]; len(v) == 4 {
			l.mask = netip.AddrFrom4([4]byte{v[0], v[1], v[2], v[3]})
		}
		if v := opts[3]; len(v) >= 4 {
			l.gw = netip.AddrFrom4([4]byte{v[0], v[1], v[2], v[3]})
		}
		return l, nil
	}
	return l, errTimeout("no ACK")
}
