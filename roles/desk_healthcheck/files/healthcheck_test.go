package main

import (
	"bytes"
	"encoding/binary"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

var members = []string{"deskb1x", "deskb2x", "deskb3x"}

const healthyCSV = "# pxname,svname,status\nxiaozhi_settings,FRONTEND,OPEN\nxiaozhi_control_planes,deskb1x,UP\nxiaozhi_control_planes,deskb2x,DOWN\nxiaozhi_control_planes,deskb3x,DOWN\n"

func TestStatsExactExistingReadiness(t *testing.T) {
	if !statsReady([]byte(healthyCSV), members) {
		t.Fatal("one healthy backend must keep VIP")
	}
	cases := []string{
		strings.ReplaceAll(healthyCSV, "UP", "DOWN"),
		strings.ReplaceAll(healthyCSV, "OPEN", "STOP"),
		strings.ReplaceAll(healthyCSV, "deskb3x", "unexpected"),
		healthyCSV + "xiaozhi_control_planes,deskb1x,UP\n",
		healthyCSV + "xiaozhi_settings,FRONTEND,OPEN\n",
		"# pxname,svname,status\nmalformed\n",
		strings.Repeat("x", maxStats+1),
	}
	for _, value := range cases {
		if statsReady([]byte(value), members) {
			t.Fatal("invalid or unavailable stats accepted")
		}
	}
	if statsReady([]byte(healthyCSV), []string{"deskb1x", "deskb1x", "deskb3x"}) {
		t.Fatal("duplicate membership accepted")
	}
}
func TestIngressSocketAndUnavailable(t *testing.T) {
	directory, err := os.MkdirTemp("/private/tmp", "desk-hc-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(directory)
	path := filepath.Join(directory, "stats.sock")
	listener, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	done := make(chan error, 1)
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			done <- err
			return
		}
		defer conn.Close()
		conn.SetDeadline(time.Now().Add(time.Second))
		raw, err := io.ReadAll(conn)
		if err == nil && !bytes.Equal(raw, []byte("show stat\n")) {
			err = unavailable
		}
		if err == nil {
			_, err = io.WriteString(conn, healthyCSV)
		}
		done <- err
	}()
	if err = ingressReady(path, members); err != nil {
		t.Fatal(err)
	}
	if err = <-done; err != nil {
		t.Fatal(err)
	}
	if err = ingressReady(path+"missing", members); err == nil {
		t.Fatal("missing socket accepted")
	}
}
func response(request []byte) []byte {
	raw := append([]byte{}, request...)
	binary.BigEndian.PutUint16(raw[2:4], 0x8180)
	binary.BigEndian.PutUint16(raw[6:8], 1)
	return append(raw, 0xc0, 0x0c, 0, 1, 0, 1, 0, 0, 0, 1, 0, 4, 127, 0, 0, 1)
}
func TestDNSPacketContractAndFailures(t *testing.T) {
	request, err := dnsRequest()
	if err != nil {
		t.Fatal(err)
	}
	raw := response(request)
	answers, err := dnsAnswers(raw, request)
	if err != nil || len(answers) != 1 || !answers[0].Equal(net.IPv4(127, 0, 0, 1)) {
		t.Fatal("valid compressed response rejected")
	}
	for _, value := range [][]byte{raw[:10], raw[:len(raw)-1], append(raw, make([]byte, 4097)...)} {
		if _, err = dnsAnswers(value, request); err == nil {
			t.Fatal("truncated/oversize response accepted")
		}
	}
	for _, offset := range []int{0, 3, 5, 12} {
		copy := append([]byte{}, raw...)
		copy[offset] ^= 1
		if _, err = dnsAnswers(copy, request); err == nil {
			t.Fatal("mismatched id, error, question count or question accepted")
		}
	}
	invalidPointer := append([]byte{}, raw...)
	invalidPointer[len(request)+1] = 255
	if _, err = dnsAnswers(invalidPointer, request); err == nil {
		t.Fatal("invalid compression pointer accepted")
	}
}
func TestDNSRealUDPAndTCPFraming(t *testing.T) {
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	port := listener.Addr().(*net.TCPAddr).Port
	udp, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: port})
	if err != nil {
		t.Fatal(err)
	}
	defer udp.Close()
	done := make(chan error, 2)
	go func() {
		buf := make([]byte, 4096)
		udp.SetDeadline(time.Now().Add(time.Second))
		size, peer, err := udp.ReadFromUDP(buf)
		if err == nil {
			_, err = udp.WriteToUDP(response(buf[:size]), peer)
		}
		done <- err
	}()
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			done <- err
			return
		}
		defer conn.Close()
		conn.SetDeadline(time.Now().Add(time.Second))
		length := make([]byte, 2)
		_, err = io.ReadFull(conn, length)
		if err != nil {
			done <- err
			return
		}
		raw := make([]byte, int(binary.BigEndian.Uint16(length)))
		_, err = io.ReadFull(conn, raw)
		if err == nil {
			result := response(raw)
			binary.BigEndian.PutUint16(length, uint16(len(result)))
			_, err = conn.Write(append(length, result...))
		}
		done <- err
	}()
	for _, tcp := range []bool{false, true} {
		if err = dnsExchange("127.0.0.1", port, tcp); err != nil {
			t.Fatal(err)
		}
	}
	for i := 0; i < 2; i++ {
		if err = <-done; err != nil {
			t.Fatal(err)
		}
	}
	if err = dnsExchange("hostname.invalid", port, false); err == nil {
		t.Fatal("hostname would add a resolver dependency")
	}
}
func TestCurrentVIPPreference(t *testing.T) {
	_, address, _ := net.ParseCIDR("192.168.1.186/24")
	address.IP = net.ParseIP("192.168.1.186")
	owner, err := ownsVIP([]byte(`{"vip":"192.168.1.186"}`), []net.Addr{address})
	if err != nil || !owner {
		t.Fatal("current owner missing")
	}
	owner, err = ownsVIP([]byte(`{"vip":"192.168.1.250"}`), []net.Addr{address})
	if err != nil || owner {
		t.Fatal("non-owner penalized")
	}
	for _, raw := range []string{`{}`, `{"vip":"::1"}`, `{"vip":"invalid"}`, "{"} {
		if _, err = ownsVIP([]byte(raw), nil); err == nil {
			t.Fatal("invalid VIP metadata accepted")
		}
	}
}
