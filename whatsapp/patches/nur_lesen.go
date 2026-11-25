package main

// Lokaler Patch: Der Assistent liest WhatsApp nur. Die REST-API der Bridge auf
// 127.0.0.1 hat keine Anmeldung, jeder Prozess auf dem Server erreicht sie, auch
// eine KI, die sich per Prompt-Injection zu einem curl überreden lässt. Senden,
// Präsenz, Medien und die Admin-Routen sind deshalb ganz abgeschaltet. Lesen,
// Status und die Kopplung (QR-Code, Telefonnummer, reconnect) bleiben.

import (
	"net/http"
	"path"
	"strings"
)

var gesperrtePraefixe = []string{
	"/api/sends",
	"/api/presence",
	"/api/admin",
	"/api/media",
}

func routeGesperrt(p string) bool {
	p = strings.ToLower(path.Clean("/" + p))
	for _, g := range gesperrtePraefixe {
		if p == g || strings.HasPrefix(p, g+"/") {
			return true
		}
	}
	return false
}

func nurLesen(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if routeGesperrt(r.URL.Path) || routeGesperrt(r.URL.RawPath) {
			writeJSON(w, http.StatusForbidden, map[string]string{"error": "route disabled: this bridge is read-only"})
			return
		}
		next.ServeHTTP(w, r)
	})
}
