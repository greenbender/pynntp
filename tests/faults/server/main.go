// Command server serves one nntp-testkit fakeserver scenario for the pynntp
// fault-injection tests (../scenarios.py).
//
//	server <scenario.json>
//
// It prints one JSON line {"addr", "port", "ca"} once listening, serves until
// stdin reaches EOF, then prints one JSON line with what the server saw
// (client lines, mismatches, dot blocks, connection count) and exits.
//
// Modes (scenario "mode"):
//
//	store   fakeserver Store + StoreOptions + Faults (RFC 3977 reader)
//	peer    fakeserver PeerHandler (IHAVE transit peer)
//	raw     byte-exact script run by this program on Conn.Raw(): each step
//	        reads one client line (unless expect is null), optionally a dot
//	        block, writes its chunks (each its own write, optionally split
//	        into pieces with pauses), then an action: close, rst, hang,
//	        starttls (TLS handshake on the open connection) or inflate (read
//	        the client's input through raw DEFLATE from now on; the server's
//	        compressed output is precomputed by the tests).
package main

import (
	"bufio"
	"compress/flate"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"math/big"
	"net"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/go-while/nntp-testkit/fakeserver"
)

type Chunk struct {
	B64        string  `json:"b64"`
	GapMS      float64 `json:"gap_ms"`       // pause before this chunk
	Split      int     `json:"split"`        // >0: write in pieces of this many bytes
	SplitGapMS float64 `json:"split_gap_ms"` // pause between pieces
}

type RawStep struct {
	Expect  *string `json:"expect"` // anchored regexp for one client line; null: do not read
	ReadDot bool    `json:"read_dot"`
	DotMax  int     `json:"dot_max"` // >0 with read_dot: read only this many lines, then Then
	Send    []Chunk `json:"send"`
	SleepMS float64 `json:"sleep_ms"` // after sending, before Then
	Then    string  `json:"then"`     // "", close, rst, hang, starttls, inflate
}

type ArticleCfg struct {
	Group   string   `json:"group"`
	Number  int64    `json:"number"`
	MsgID   string   `json:"msgid"`
	Headers []string `json:"headers"`
	Body    []string `json:"body"`
}

type StoreCfg struct {
	Groups   [][2]string  `json:"groups"` // name, description
	Articles []ArticleCfg `json:"articles"`
	User     string       `json:"user"`
	Pass     string       `json:"pass"`
	Reader   bool         `json:"reader_only"`
	Compress bool         `json:"compress"`
	CompOut  bool         `json:"compress_output_only"`
	LGRange  string       `json:"listgroup_range"`
	NoOver   bool         `json:"no_over"`
	NoXover  bool         `json:"no_xover"`
}

type PeerCfg struct {
	User     string          `json:"user"`
	Pass     string          `json:"pass"`
	Have     map[string]bool `json:"have"`
	Defer436 map[string]bool `json:"defer436"`
	Reject   map[string]bool `json:"reject"`
	Compress bool            `json:"compress"`
}

type FaultsCfg struct {
	DelayMS       map[string]float64  `json:"delay_ms"`
	CloseAfter    int                 `json:"close_after"`
	Override      map[string][]string `json:"override"`
	OverrideLine  map[string][]string `json:"override_line"`
	Drop          map[string]int      `json:"drop_in_multiline"`
	DripMS        map[string]float64  `json:"drip_ms"`
	Endless       map[string]bool     `json:"endless"`
	LineDelayMS   map[string]float64  `json:"line_delay_ms"`
	OverrideFrom  map[string]int      `json:"override_from"`
	OverrideTimes map[string]int      `json:"override_times"`
}

type Config struct {
	Mode       string     `json:"mode"`
	TLS        bool       `json:"tls"` // implicit TLS on every connection
	Greeting   string     `json:"greeting"`
	NoGreeting bool       `json:"no_greeting"`
	RawStall   bool       `json:"raw_stall"`
	DeadlineMS float64    `json:"deadline_ms"`
	Store      *StoreCfg  `json:"store"`
	Peer       *PeerCfg   `json:"peer"`
	Faults     *FaultsCfg `json:"faults"`
	Raw        []RawStep  `json:"raw"`
}

func ms(f float64) time.Duration { return time.Duration(f * float64(time.Millisecond)) }

func msMap(m map[string]float64) map[string]time.Duration {
	if m == nil {
		return nil
	}
	out := make(map[string]time.Duration, len(m))
	for k, v := range m {
		out[k] = ms(v)
	}
	return out
}

// recorder holds what raw mode saw (the fakeserver records the other modes).
type recorder struct {
	mu         sync.Mutex
	lines      []string
	mismatches []string
	articles   [][]string
	notes      []string
}

func (r *recorder) add(dst *[]string, s string) { r.mu.Lock(); *dst = append(*dst, s); r.mu.Unlock() }

func readLine(r *bufio.Reader) (string, error) {
	s, err := r.ReadString('\n')
	if err != nil {
		return "", err
	}
	return strings.TrimRight(s, "\r\n"), nil
}

func writeChunk(w io.Writer, ch Chunk) error {
	data, err := base64.StdEncoding.DecodeString(ch.B64)
	if err != nil {
		return err
	}
	time.Sleep(ms(ch.GapMS))
	if ch.Split <= 0 {
		_, err = w.Write(data)
		return err
	}
	for i := 0; i < len(data); i += ch.Split {
		if i > 0 {
			time.Sleep(ms(ch.SplitGapMS))
		}
		if _, err = w.Write(data[i:min(i+ch.Split, len(data))]); err != nil {
			return err
		}
	}
	return nil
}

func rawHandler(steps []RawStep, rec *recorder, tlsCfg *tls.Config) func(*fakeserver.Conn) {
	res := make([]*regexp.Regexp, len(steps))
	for i, st := range steps {
		if st.Expect != nil {
			res[i] = regexp.MustCompile("^(?:" + *st.Expect + ")$")
		}
	}
	return func(c *fakeserver.Conn) {
		conn := c.Raw()
		tcp := func() *net.TCPConn {
			nc := conn
			if t, ok := nc.(*tls.Conn); ok {
				nc = t.NetConn()
			}
			t, _ := nc.(*net.TCPConn)
			return t
		}
		if t := tcp(); t != nil {
			t.SetNoDelay(true)
		}
		r := bufio.NewReader(conn)
		var w io.Writer = conn
		for i, st := range steps {
			if res[i] != nil {
				for {
					line, err := readLine(r)
					if err != nil {
						return
					}
					rec.add(&rec.lines, line)
					if res[i].MatchString(line) {
						break
					}
					rec.add(&rec.mismatches, line)
					fmt.Fprintf(w, "500 unexpected %s\r\n", line)
				}
			}
			if st.ReadDot {
				var block []string
				for {
					line, err := readLine(r)
					if err != nil {
						rec.add(&rec.notes, fmt.Sprintf("dot block read: %v after %d lines", err, len(block)))
						return
					}
					block = append(block, line)
					if line == "." || (st.DotMax > 0 && len(block) >= st.DotMax) {
						break
					}
				}
				rec.mu.Lock()
				rec.articles = append(rec.articles, block)
				rec.mu.Unlock()
			}
			for _, ch := range st.Send {
				if err := writeChunk(w, ch); err != nil {
					rec.add(&rec.notes, "write: "+err.Error())
					return
				}
			}
			time.Sleep(ms(st.SleepMS))
			switch st.Then {
			case "close":
				return
			case "rst":
				if t := tcp(); t != nil {
					t.SetLinger(0)
				}
				return
			case "hang":
				for {
					line, err := readLine(r)
					if err != nil {
						return
					}
					rec.add(&rec.lines, line)
				}
			case "starttls":
				tc := tls.Server(conn, tlsCfg)
				tc.SetDeadline(time.Now().Add(10 * time.Second))
				if err := tc.Handshake(); err != nil {
					rec.add(&rec.notes, "starttls handshake: "+err.Error())
					return
				}
				tc.SetDeadline(time.Time{})
				conn = tc
				r = bufio.NewReader(conn)
				w = conn
			case "inflate":
				r = bufio.NewReader(flate.NewReader(r))
			case "":
			default:
				rec.add(&rec.notes, "unknown then: "+st.Then)
				return
			}
		}
		for { // after the script: record, answer 500
			line, err := readLine(r)
			if err != nil {
				return
			}
			rec.add(&rec.lines, line)
			rec.add(&rec.mismatches, line)
			fmt.Fprintf(w, "500 unexpected %s\r\n", line)
		}
	}
}

// selfSigned makes a cert for 127.0.0.1 (fakeserver.TLSConfig needs a testing.TB).
func selfSigned() (*tls.Config, []byte, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, err
	}
	serial, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 62))
	now := time.Now()
	tmpl := &x509.Certificate{
		SerialNumber: serial, Subject: pkix.Name{CommonName: "pynntp-faults"},
		NotBefore: now.Add(-time.Hour), NotAfter: now.Add(24 * time.Hour),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true, IsCA: true,
		IPAddresses: []net.IP{net.ParseIP("127.0.0.1")}, DNSNames: []string{"localhost"},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		return nil, nil, err
	}
	cfg := &tls.Config{Certificates: []tls.Certificate{{Certificate: [][]byte{der}, PrivateKey: key}}, MinVersion: tls.VersionTLS12}
	return cfg, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), nil
}

func buildStore(sc *StoreCfg) *fakeserver.Store {
	st := fakeserver.NewStore()
	for _, g := range sc.Groups {
		st.AddGroupAt(g[0], g[1], time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC))
	}
	for _, a := range sc.Articles {
		st.AddArticle(a.Group, fakeserver.Article{
			Number: a.Number, MessageID: a.MsgID, Headers: a.Headers, Body: a.Body,
			Date: time.Date(2020, 1, 2, 3, 4, 5, 0, time.UTC), Arrived: time.Date(2020, 1, 2, 3, 4, 5, 0, time.UTC),
		})
	}
	return st
}

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: server <scenario.json>")
		os.Exit(2)
	}
	raw, err := os.ReadFile(os.Args[1])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	var cfg Config
	if err := json.Unmarshal(raw, &cfg); err != nil {
		fmt.Fprintln(os.Stderr, "scenario:", err)
		os.Exit(2)
	}
	tlsCfg, caPEM, err := selfSigned()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	ca, err := os.CreateTemp("", "pynntp-faults-ca-*.pem")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	ca.Write(caPEM)
	ca.Close()
	defer os.Remove(ca.Name())

	o := fakeserver.Options{
		Greeting: cfg.Greeting, NoGreeting: cfg.NoGreeting, RawStall: cfg.RawStall,
		Deadline: ms(cfg.DeadlineMS),
	}
	if o.Deadline == 0 {
		o.Deadline = 10 * time.Second
	}
	if cfg.TLS {
		o.TLS = tlsCfg
	}
	if f := cfg.Faults; f != nil {
		o.Faults = fakeserver.Faults{
			Delay: msMap(f.DelayMS), CloseAfter: f.CloseAfter, Override: f.Override,
			OverrideLine: f.OverrideLine, DropInMultiline: f.Drop, Drip: msMap(f.DripMS),
			Endless: f.Endless, LineDelay: msMap(f.LineDelayMS),
			OverrideFrom: f.OverrideFrom, OverrideTimes: f.OverrideTimes,
		}
	}
	rec := &recorder{}
	switch cfg.Mode {
	case "store":
		sc := cfg.Store
		if sc == nil {
			sc = &StoreCfg{}
		}
		o.Store = buildStore(sc)
		o.StoreOpts = fakeserver.StoreOptions{
			User: sc.User, Pass: sc.Pass, ReaderOnly: sc.Reader, Compress: sc.Compress,
			CompressOutputOnly: sc.CompOut, ListgroupRange: sc.LGRange, NoOver: sc.NoOver, NoXover: sc.NoXover,
		}
	case "peer":
		p := cfg.Peer
		if p == nil {
			p = &PeerCfg{}
		}
		o.Handler = fakeserver.PeerHandler(&fakeserver.PeerOptions{
			User: p.User, Pass: p.Pass, Have: p.Have, Defer436Once: p.Defer436, Reject: p.Reject, Compress: p.Compress,
		})
	case "raw":
		o.NoGreeting = true // the script writes the greeting
		o.Handler = rawHandler(cfg.Raw, rec, tlsCfg)
	default:
		fmt.Fprintln(os.Stderr, "unknown mode", cfg.Mode)
		os.Exit(2)
	}
	srv, err := fakeserver.Start(o)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	out := json.NewEncoder(os.Stdout)
	out.Encode(map[string]any{"addr": srv.Addr(), "host": srv.Host(), "port": srv.Port(), "ca": ca.Name()})

	io.Copy(io.Discard, os.Stdin) // the tests close stdin when the client is done
	accepted := srv.Accepted()
	srv.Close()
	rec.mu.Lock()
	defer rec.mu.Unlock()
	lines, mism, arts := srv.Lines(), srv.Mismatches(), srv.Articles()
	if cfg.Mode == "raw" {
		lines, mism, arts = rec.lines, rec.mismatches, rec.articles
	}
	out.Encode(map[string]any{
		"lines": nz(lines), "mismatches": nz(mism), "articles": arts, "notes": nz(rec.notes),
		"accepted": accepted, "counts": srv.Counts(),
	})
}

func nz(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}
