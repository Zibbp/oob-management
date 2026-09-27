package main

import (
	"crypto/subtle"
	_ "embed"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"

	"tinygo.org/x/drivers/w5500"
)

//go:embed ui.html
var uiHTML string

type pcStatus struct {
	ID      int    `json:"id"`
	Label   string `json:"label"`
	PowerOn bool   `json:"power_on"`
	AuxOn   bool   `json:"aux_on"`
}

func checkAuth(r *http.Request) bool {
	storeMu.Lock()
	u, p := authUser, authPass
	storeMu.Unlock()
	auth := r.Header.Get("Authorization")
	if !strings.HasPrefix(auth, "Basic ") {
		return false
	}
	dec, err := base64.StdEncoding.DecodeString(auth[6:])
	if err != nil {
		return false
	}
	parts := strings.SplitN(string(dec), ":", 2)
	if len(parts) != 2 {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(parts[0]), []byte(u)) == 1 &&
		subtle.ConstantTimeCompare([]byte(parts[1]), []byte(p)) == 1
}

func withAuth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !checkAuth(r) {
			w.Header().Set("WWW-Authenticate", `Basic realm="OOB Management"`)
			http.Error(w, "Unauthorized\n", http.StatusUnauthorized)
			logAdd("auth failed for %s %s", r.Method, r.URL.Path)
			return
		}
		next(w, r)
	}
}

func param(r *http.Request, key string) string {
	if v := r.URL.Query().Get(key); v != "" {
		return v
	}
	return r.FormValue(key)
}

func handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" && r.URL.Path != "/index.html" {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	fmt.Fprint(w, uiHTML)
}

func handleStatus(w http.ResponseWriter, r *http.Request) {
	pcs := make([]pcStatus, 0, len(PCS))
	for i, pc := range PCS {
		pcs = append(pcs, pcStatus{
			ID:      pc.ID,
			Label:   labels[i],
			PowerOn: mcpRead(pc.Bank, pc.Led),
			AuxOn:   mcpRead(pc.Bank, pc.Aux),
		})
	}
	link := ethDev != nil && ethDev.LinkStatus() == w5500.LinkStatusUp
	out, _ := json.Marshal(map[string]any{
		"app":      AppName,
		"version":  AppVersion,
		"hostname": Hostname,
		"ip":       currentIP,
		"link":     link,
		"pcs":      pcs,
	})
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Write(out)
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	fmt.Fprintf(w, `{"ok":true,"version":%q}`, AppVersion)
}

func handleLog(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Write([]byte(logText()))
}

func handleSerial(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Write([]byte(serialText()))
}

func handleSerialSend(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		http.Error(w, "method not allowed\n", http.StatusMethodNotAllowed)
		return
	}
	r.ParseForm()
	if err := serialSend(param(r, "text")); err != nil {
		http.Error(w, "invalid text\n", http.StatusBadRequest)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{"ok":true}`))
}

func handleSerialClear(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		http.Error(w, "method not allowed\n", http.StatusMethodNotAllowed)
		return
	}
	serialClear()
	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{"ok":true}`))
}

func handleAction(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		http.Error(w, "method not allowed\n", http.StatusMethodNotAllowed)
		return
	}
	r.ParseForm()
	id, _ := strconv.Atoi(param(r, "pc"))
	action := param(r, "action")
	if action == "" {
		action = param(r, "type")
	}
	logAdd("action pc=%d %s", id, action)
	if err := runAction(id, action); err != nil {
		http.Error(w, err.Error()+"\n", http.StatusBadRequest)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{"ok":true}`))
}

func handleKVM(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		http.Error(w, "method not allowed\n", http.StatusMethodNotAllowed)
		return
	}
	r.ParseForm()
	port, _ := strconv.Atoi(param(r, "port"))
	logAdd("KVM port=%d", port)
	if err := kvmSwitch(port); err != nil {
		http.Error(w, err.Error()+"\n", http.StatusBadRequest)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{"ok":true}`))
}

func handleLabel(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		http.Error(w, "method not allowed\n", http.StatusMethodNotAllowed)
		return
	}
	r.ParseForm()
	id, _ := strconv.Atoi(param(r, "pc"))
	name := strings.TrimSpace(param(r, "name"))
	if findPC(id) == nil || name == "" || len(name) > labelLen {
		http.Error(w, "invalid label\n", http.StatusBadRequest)
		return
	}
	old := labelFor(id)
	if !setLabel(id, name) {
		http.Error(w, "unknown PC\n", http.StatusBadRequest)
		return
	}
	if err := storeSave(); err != nil {
		setLabel(id, old)
		http.Error(w, "save failed\n", http.StatusInternalServerError)
		return
	}
	logAdd("label pc=%d %q -> %q", id, old, name)
	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{"ok":true}`))
}

func handleAuthChange(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		http.Error(w, "method not allowed\n", http.StatusMethodNotAllowed)
		return
	}
	r.ParseForm()
	user := strings.TrimSpace(param(r, "user"))
	pass := param(r, "pass")
	if len(user) < 1 || len(user) > userLen || len(pass) < 1 || len(pass) > passLen {
		http.Error(w, "invalid credentials\n", http.StatusBadRequest)
		return
	}
	storeMu.Lock()
	ou, op := authUser, authPass
	authUser, authPass = user, pass
	storeMu.Unlock()
	if err := storeSave(); err != nil {
		storeMu.Lock()
		authUser, authPass = ou, op
		storeMu.Unlock()
		http.Error(w, "save failed\n", http.StatusInternalServerError)
		return
	}
	logAdd("auth user changed to %q", user)
	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{"ok":true}`))
}

func routes() *http.ServeMux {
	m := http.NewServeMux()
	m.HandleFunc("/", withAuth(handleIndex))
	m.HandleFunc("/status", withAuth(handleStatus))
	m.HandleFunc("/health", handleHealth)
	m.HandleFunc("/log", withAuth(handleLog))
	m.HandleFunc("/serial", withAuth(handleSerial))
	m.HandleFunc("/serial/send", withAuth(handleSerialSend))
	m.HandleFunc("/serial/clear", withAuth(handleSerialClear))
	m.HandleFunc("/favicon.ico", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})
	m.HandleFunc("/action", withAuth(handleAction))
	m.HandleFunc("/kvm", withAuth(handleKVM))
	m.HandleFunc("/label", withAuth(handleLabel))
	m.HandleFunc("/setlabel", withAuth(handleLabel))
	m.HandleFunc("/auth", withAuth(handleAuthChange))
	return m
}
