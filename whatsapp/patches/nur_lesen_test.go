package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestNurLesen(t *testing.T) {
	mux := http.NewServeMux()
	ok := func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) }
	for _, r := range []string{"POST /api/sends", "POST /api/sends/{id}/confirm", "POST /api/presence/typing",
		"POST /api/admin/request-history", "POST /api/media/download", "GET /api/status", "GET /api/messages",
		"POST /api/auth/pair-phone", "GET /api/auth/qr"} {
		mux.HandleFunc(r, ok)
	}
	h := nurLesen(mux)
	faelle := []struct {
		methode, pfad string
		code          int
	}{
		{"POST", "/api/sends", http.StatusForbidden},
		{"POST", "/api/sends/abc/confirm", http.StatusForbidden},
		{"POST", "/api//sends", http.StatusForbidden},
		{"POST", "/API/Sends", http.StatusForbidden},
		{"POST", "/api/x/../sends", http.StatusForbidden},
		{"POST", "/api/presence/typing", http.StatusForbidden},
		{"POST", "/api/admin/request-history", http.StatusForbidden},
		{"POST", "/api/media/download", http.StatusForbidden},
		{"GET", "/api/status", http.StatusOK},
		{"GET", "/api/messages", http.StatusOK},
		{"POST", "/api/auth/pair-phone", http.StatusOK},
		{"GET", "/api/auth/qr", http.StatusOK},
	}
	for _, f := range faelle {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest(f.methode, f.pfad, nil))
		if w.Code != f.code {
			t.Errorf("%s %s: %d, erwartet %d", f.methode, f.pfad, w.Code, f.code)
		}
	}
}
