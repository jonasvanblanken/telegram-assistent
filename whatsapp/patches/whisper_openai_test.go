package main

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

// newOpenAITranscriber wires a Transcriber with just the config the OpenAI
// path reads. NewTranscriber is avoided on purpose: it starts worker
// goroutines this test has no use for.
func newOpenAITranscriber(t *testing.T, lang, model string) *Transcriber {
	t.Helper()
	return &Transcriber{cfg: &Config{
		WhisperBackend:  "openai-api",
		WhisperAPIKey:   "sk-test-key",
		WhisperLanguage: lang,
		WhisperAPIModel: model,
	}}
}

func writeAudio(t *testing.T, size int) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "audio.ogg")
	if err := os.WriteFile(p, make([]byte, size), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

func serveOpenAI(t *testing.T, h http.HandlerFunc) {
	t.Helper()
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	prev := openAITranscriptionURL
	openAITranscriptionURL = srv.URL
	t.Cleanup(func() { openAITranscriptionURL = prev })
}

func TestTranscribeOpenAISendsExpectedRequest(t *testing.T) {
	var gotAuth, gotModel, gotFormat, gotFilename string
	var langPresent bool
	var fileLen int64

	serveOpenAI(t, func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		if err := r.ParseMultipartForm(32 << 20); err != nil {
			t.Errorf("ParseMultipartForm: %v", err)
		}
		gotModel = r.FormValue("model")
		gotFormat = r.FormValue("response_format")
		_, langPresent = r.MultipartForm.Value["language"]
		f, hdr, err := r.FormFile("file")
		if err != nil {
			t.Errorf("FormFile: %v", err)
		} else {
			defer f.Close()
			gotFilename = hdr.Filename
			fileLen = hdr.Size
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"text":"  Hallo, hier ist der Axel.  "}`)
	})

	tr := newOpenAITranscriber(t, "de", "")
	got, err := tr.transcribeOpenAI(context.Background(), writeAudio(t, 2048))
	if err != nil {
		t.Fatalf("transcribeOpenAI: %v", err)
	}

	if got != "Hallo, hier ist der Axel." {
		t.Errorf("transcript = %q, want trimmed text", got)
	}
	if gotAuth != "Bearer sk-test-key" {
		t.Errorf("Authorization = %q", gotAuth)
	}
	if gotModel != "whisper-1" {
		t.Errorf("model = %q, want the whisper-1 default", gotModel)
	}
	if gotFormat != "json" {
		t.Errorf("response_format = %q", gotFormat)
	}
	if !langPresent {
		t.Error("language field missing though WHATSAPP_WHISPER_LANGUAGE=de")
	}
	if gotFilename != "audio.ogg" {
		t.Errorf("filename = %q, want the Opus file (not a converted wav)", gotFilename)
	}
	if fileLen != 2048 {
		t.Errorf("uploaded %d bytes, want 2048", fileLen)
	}
}

// The mixed-language case: an explicit language is a hard constraint at the
// API, so "auto" has to mean "omit the field" or every English voice note
// comes back forced through German.
func TestTranscribeOpenAIOmitsLanguageOnAuto(t *testing.T) {
	for _, lang := range []string{"auto", "", "  AUTO  ", "detect"} {
		t.Run("lang="+lang, func(t *testing.T) {
			var present bool
			serveOpenAI(t, func(w http.ResponseWriter, r *http.Request) {
				if err := r.ParseMultipartForm(32 << 20); err != nil {
					t.Errorf("ParseMultipartForm: %v", err)
				}
				_, present = r.MultipartForm.Value["language"]
				fmt.Fprint(w, `{"text":"ok"}`)
			})
			tr := newOpenAITranscriber(t, lang, "")
			if _, err := tr.transcribeOpenAI(context.Background(), writeAudio(t, 16)); err != nil {
				t.Fatal(err)
			}
			if present {
				t.Errorf("language field was sent for %q; autodetect must omit it", lang)
			}
		})
	}
}

func TestTranscribeOpenAICustomModel(t *testing.T) {
	var gotModel string
	serveOpenAI(t, func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseMultipartForm(32 << 20)
		gotModel = r.FormValue("model")
		fmt.Fprint(w, `{"text":"ok"}`)
	})
	tr := newOpenAITranscriber(t, "auto", "gpt-4o-transcribe")
	if _, err := tr.transcribeOpenAI(context.Background(), writeAudio(t, 16)); err != nil {
		t.Fatal(err)
	}
	if gotModel != "gpt-4o-transcribe" {
		t.Errorf("model = %q, want the configured override", gotModel)
	}
}

func TestTranscribeOpenAIRetriesTransientThenSucceeds(t *testing.T) {
	var calls int32
	serveOpenAI(t, func(w http.ResponseWriter, r *http.Request) {
		if atomic.AddInt32(&calls, 1) == 1 {
			w.WriteHeader(http.StatusTooManyRequests)
			fmt.Fprint(w, `{"error":{"message":"Rate limit reached","code":"rate_limit_exceeded"}}`)
			return
		}
		fmt.Fprint(w, `{"text":"zweiter Versuch"}`)
	})
	tr := newOpenAITranscriber(t, "auto", "")
	got, err := tr.transcribeOpenAI(context.Background(), writeAudio(t, 16))
	if err != nil {
		t.Fatalf("expected the retry to succeed: %v", err)
	}
	if got != "zweiter Versuch" {
		t.Errorf("transcript = %q", got)
	}
	if calls != 2 {
		t.Errorf("server saw %d calls, want exactly 2", calls)
	}
}

// A bad API key is permanent. Retrying it wastes the job's 10-minute budget
// and blocks the single transcription worker for every later voice note.
func TestTranscribeOpenAIDoesNotRetryAuthFailure(t *testing.T) {
	var calls int32
	serveOpenAI(t, func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		w.WriteHeader(http.StatusUnauthorized)
		fmt.Fprint(w, `{"error":{"message":"Incorrect API key provided","code":"invalid_api_key"}}`)
	})
	tr := newOpenAITranscriber(t, "auto", "")
	_, err := tr.transcribeOpenAI(context.Background(), writeAudio(t, 16))
	if err == nil {
		t.Fatal("expected an error on HTTP 401")
	}
	if calls != 1 {
		t.Errorf("server saw %d calls, want exactly 1 (no retry on 401)", calls)
	}
	if !strings.Contains(err.Error(), "Incorrect API key provided") {
		t.Errorf("error should surface the API's message, got: %v", err)
	}
	if strings.Contains(err.Error(), "sk-test-key") {
		t.Errorf("error leaked the API key: %v", err)
	}
}

func TestTranscribeOpenAIRejectsOversizeBeforeUpload(t *testing.T) {
	var calls int32
	serveOpenAI(t, func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		fmt.Fprint(w, `{"text":"should not get here"}`)
	})
	tr := newOpenAITranscriber(t, "auto", "")
	_, err := tr.transcribeOpenAI(context.Background(), writeAudio(t, openAIMaxUploadBytes+1))
	if err == nil {
		t.Fatal("expected an error for an oversize file")
	}
	if !strings.Contains(err.Error(), "upload limit") {
		t.Errorf("error should name the upload limit, got: %v", err)
	}
	if calls != 0 {
		t.Errorf("oversize file was uploaded anyway (%d calls)", calls)
	}
}

func TestTranscribeOpenAIRejectsEmptyFile(t *testing.T) {
	tr := newOpenAITranscriber(t, "auto", "")
	if _, err := tr.transcribeOpenAI(context.Background(), writeAudio(t, 0)); err == nil {
		t.Fatal("expected an error for an empty audio file")
	}
}
