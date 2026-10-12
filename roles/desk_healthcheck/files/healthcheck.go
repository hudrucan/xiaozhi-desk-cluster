// Native bounded Keepalived probes. Deployment verification stays in Python.
package main

import (
	"bytes"
	"crypto/rand"
	"encoding/binary"
	"encoding/csv"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"net"
	"os"
	"strconv"
	"strings"
	"time"
)

const maxStats = 1024 * 1024

var unavailable = errors.New("healthcheck unavailable")

func statsReady(payload []byte, expected []string) bool {
	if len(payload) == 0 || len(payload) > maxStats || len(expected) != 3 {
		return false
	}
	wanted := map[string]bool{}
	for _, node := range expected {
		if node == "" || wanted[node] {
			return false
		}
		wanted[node] = true
	}
	reader := csv.NewReader(bytes.NewReader(bytes.TrimPrefix(payload, []byte("# "))))
	header, err := reader.Read()
	if err != nil {
		return false
	}
	columns := map[string]int{}
	for i, name := range header {
		if _, exists := columns[name]; exists {
			return false
		}
		columns[name] = i
	}
	for _, name := range []string{"pxname", "svname", "status"} {
		if _, exists := columns[name]; !exists {
			return false
		}
	}
	front, healthy := 0, 0
	found := map[string]bool{}
	for {
		row, err := reader.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			return false
		}
		backend, server, status := row[columns["pxname"]], row[columns["svname"]], row[columns["status"]]
		if backend == "xiaozhi_settings" && server == "FRONTEND" {
			front++
			if status != "OPEN" {
				return false
			}
		}
		if backend == "xiaozhi_control_planes" && wanted[server] {
			if found[server] {
				return false
			}
			found[server] = true
			if status == "UP" {
				healthy++
			}
		}
	}
	return front == 1 && len(found) == len(wanted) && healthy > 0
}

func ingressReady(path string, nodes []string) error {
	if !strings.HasPrefix(path, "/") {
		return unavailable
	}
	deadline := time.Now().Add(2 * time.Second)
	conn, err := net.DialTimeout("unix", path, 2*time.Second)
	if err != nil {
		return err
	}
	defer conn.Close()
	if err = conn.SetDeadline(deadline); err != nil {
		return err
	}
	if _, err = io.WriteString(conn, "show stat\n"); err != nil {
		return err
	}
	if unix, ok := conn.(*net.UnixConn); ok {
		if err = unix.CloseWrite(); err != nil {
			return err
		}
	}
	payload, err := io.ReadAll(io.LimitReader(conn, maxStats+1))
	if err != nil {
		return err
	}
	if !statsReady(payload, nodes) {
		return unavailable
	}
	return nil
}

func dnsRequest() ([]byte, error) {
	packet := make([]byte, 12)
	if _, err := rand.Read(packet[:2]); err != nil {
		return nil, err
	}
	binary.BigEndian.PutUint16(packet[2:4], 0x0100)
	binary.BigEndian.PutUint16(packet[4:6], 1)
	for _, label := range strings.Split("frontend.desk-dns.test", ".") {
		packet = append(packet, byte(len(label)))
		packet = append(packet, label...)
	}
	return append(packet, 0, 0, 1, 0, 1), nil
}

func skipName(data []byte, pos int) (int, error) {
	for i := 0; i < 128; i++ {
		if pos >= len(data) {
			return 0, unavailable
		}
		size := int(data[pos])
		if size&0xc0 == 0xc0 {
			if pos+1 >= len(data) || int(binary.BigEndian.Uint16(data[pos:pos+2])&0x3fff) >= len(data) {
				return 0, unavailable
			}
			return pos + 2, nil
		}
		if size&0xc0 != 0 {
			return 0, unavailable
		}
		pos++
		if size == 0 {
			return pos, nil
		}
		pos += size
	}
	return 0, unavailable
}

func dnsAnswers(data, request []byte) ([]net.IP, error) {
	if len(data) < 12 || len(data) > 4096 || len(request) < 12 {
		return nil, unavailable
	}
	flags := binary.BigEndian.Uint16(data[2:4])
	answers := int(binary.BigEndian.Uint16(data[6:8]))
	if !bytes.Equal(data[:2], request[:2]) || flags&0x8000 == 0 || flags&0x7a0f != 0 || binary.BigEndian.Uint16(data[4:6]) != 1 || answers > 32 {
		return nil, unavailable
	}
	pos, err := skipName(data, 12)
	if err != nil {
		return nil, err
	}
	pos += 4
	if pos > len(data) || !bytes.Equal(data[12:pos], request[12:]) {
		return nil, unavailable
	}
	result := []net.IP{}
	for i := 0; i < answers; i++ {
		pos, err = skipName(data, pos)
		if err != nil || pos+10 > len(data) {
			return nil, unavailable
		}
		typ, cls, size := binary.BigEndian.Uint16(data[pos:pos+2]), binary.BigEndian.Uint16(data[pos+2:pos+4]), int(binary.BigEndian.Uint16(data[pos+8:pos+10]))
		pos += 10
		if pos+size > len(data) {
			return nil, unavailable
		}
		if typ == 1 && cls == 1 && size == 4 {
			result = append(result, net.IP(data[pos:pos+4]))
		}
		pos += size
	}
	return result, nil
}

func dnsExchange(host string, port int, tcp bool) error {
	// The production frontend readiness check uses its numeric management IPv4.
	ip := net.ParseIP(host)
	if ip == nil || ip.To4() == nil || port < 1 || port > 65535 {
		return unavailable
	}
	request, err := dnsRequest()
	if err != nil {
		return err
	}
	network := "udp4"
	if tcp {
		network = "tcp4"
	}
	deadline := time.Now().Add(2 * time.Second)
	conn, err := net.DialTimeout(network, net.JoinHostPort(host, strconv.Itoa(port)), 2*time.Second)
	if err != nil {
		return err
	}
	defer conn.Close()
	if err = conn.SetDeadline(deadline); err != nil {
		return err
	}
	var data []byte
	if tcp {
		length := make([]byte, 2)
		binary.BigEndian.PutUint16(length, uint16(len(request)))
		if _, err = conn.Write(append(length, request...)); err != nil {
			return err
		}
		if _, err = io.ReadFull(conn, length); err != nil {
			return err
		}
		size := int(binary.BigEndian.Uint16(length))
		if size < 12 || size > 4096 {
			return unavailable
		}
		data = make([]byte, size)
		if _, err = io.ReadFull(conn, data); err != nil {
			return err
		}
	} else {
		if _, err = conn.Write(request); err != nil {
			return err
		}
		data = make([]byte, 4097)
		size, err := conn.Read(data)
		if err != nil {
			return err
		}
		data = data[:size]
	}
	answers, err := dnsAnswers(data, request)
	if err != nil {
		return err
	}
	if len(answers) != 1 || !answers[0].Equal(net.IPv4(127, 0, 0, 1)) {
		return unavailable
	}
	return nil
}

func ownsVIP(raw []byte, addresses []net.Addr) (bool, error) {
	if len(raw) > 65536 {
		return false, unavailable
	}
	var state struct {
		VIP string `json:"vip"`
	}
	if err := json.Unmarshal(raw, &state); err != nil {
		return false, unavailable
	}
	vip := net.ParseIP(state.VIP)
	if vip == nil || vip.To4() == nil {
		return false, unavailable
	}
	for _, address := range addresses {
		ip, _, err := net.ParseCIDR(address.String())
		if err != nil {
			continue
		}
		if ip.Equal(vip) {
			return true, nil
		}
	}
	return false, nil
}

func run(args []string) error {
	if len(args) == 0 {
		return unavailable
	}
	flags := flag.NewFlagSet("desk-healthcheck", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	socket := flags.String("socket", "/run/xiaozhi-haproxy.sock", "")
	nodes := flags.String("nodes", "deskb1x,deskb2x,deskb3x", "")
	host := flags.String("host", "", "")
	port := flags.Int("port", 5353, "")
	state := flags.String("state", "/etc/xiaozhi-ingress.json", "")
	if err := flags.Parse(args[1:]); err != nil || flags.NArg() != 0 {
		return unavailable
	}
	switch args[0] {
	case "ingress":
		return ingressReady(*socket, strings.Split(*nodes, ","))
	case "dns":
		if err := dnsExchange(*host, *port, false); err != nil {
			return err
		}
		return dnsExchange(*host, *port, true)
	case "prefer":
		file, err := os.Open(*state)
		if err != nil {
			return err
		}
		defer file.Close()
		raw, err := io.ReadAll(io.LimitReader(file, 65537))
		if err != nil {
			return err
		}
		addresses, err := net.InterfaceAddrs()
		if err != nil {
			return err
		}
		owner, err := ownsVIP(raw, addresses)
		if err != nil {
			return err
		}
		if owner {
			return unavailable
		}
		return nil
	default:
		return unavailable
	}
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		os.Exit(1)
	}
}
