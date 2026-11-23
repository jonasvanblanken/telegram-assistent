// whisper_openai.go — OpenAI transcription backend.
//
// LOCAL PATCH, not upstream. Upstream ships transcribeOpenAI as a stub that
// returns "openai-api backend scaffold: implementation pending" for every job,
// while Config.Validate happily accepts WHATSAPP_WHISPER_BACKEND=openai-api as
// long as an API key is set. The bridge therefore boots clean and then drops
// every single voice note with only a log line — the exact silent failure this
// deployment cannot have, since a missing transcript is indistinguishable from
// "no voice notes arrived".
//
// Kept in its own file so re-applying it after an upstream update is a copy
// plus one deletion (see the deploy README).

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const (
	// OpenAI rejects uploads over 25 MB. Checked before the request so the
	// failure names the real cause instead of surfacing as a generic 413.
	openAIMaxUploadBytes = 25 << 20

	openAIAttempts = 3
)

// openAITranscriptionURL is a var, not a const, purely so the tests can point
// it at an httptest server. Nothing at runtime reassigns it.
var openAITranscriptionURL = "https://api.openai.com/v1/audio/transcriptions"

// openAIHTTPClient is package-level so connections are reused across jobs.
// The per-request deadline comes from the caller's context (10 min per job);
// this timeout is the backstop for a connection that never completes.
var openAIHTTPClient = &http.Client{Timeout: 5 * time.Minute}

// transcribeOpenAI uploads one audio file to OpenAI's transcription endpoint
// and returns the plain-text transcript.
//
// audioPath is the original Opus file, not the WAV whisper.cpp needs: the
// endpoint accepts ogg/opus natively, the container is roughly 15x smaller
// than 16 kHz mono PCM, and skipping the conversion keeps long voice notes
// under the upload ceiling while removing the ffmpeg dependency for this
// backend. See the backend switch in Transcriber.process.
func (t *Transcriber) transcribeOpenAI(ctx context.Context, audioPath string) (string, error) {
	info, err := os.Stat(audioPath)
	if err != nil {
		return "", fmt.Errorf("stat audio: %w", err)
	}
	if info.Size() == 0 {
		return "", errors.New("audio file is empty")
	}
	if info.Size() > openAIMaxUploadBytes {
		return "", fmt.Errorf("audio is %d bytes, over OpenAI's %d-byte upload limit", info.Size(), openAIMaxUploadBytes)
	}

	body, contentType, err := t.buildOpenAIRequest(audioPath)
	if err != nil {
		return "", err
	}

	var lastErr error
	for attempt := 1; attempt <= openAIAttempts; attempt++ {
		if attempt > 1 {
			// Linear backoff, cancellable. Bounded attempts matter: the job
			// context expires after 10 minutes and a wedged retry loop would
			// block the single transcription worker for every later voice note.
			select {
			case <-ctx.Done():
				return "", ctx.Err()
			case <-time.After(time.Duration(attempt-1) * 3 * time.Second):
			}
		}

		text, retryable, err := t.postOpenAITranscription(ctx, bytes.NewReader(body), contentType)
		if err == nil {
			return text, nil
		}
		lastErr = err
		if !retryable {
			return "", err
		}
	}
	return "", fmt.Errorf("openai transcription failed after %d attempts: %w", openAIAttempts, lastErr)
}

// buildOpenAIRequest assembles the multipart body once so retries do not
// re-read the file off disk.
func (t *Transcriber) buildOpenAIRequest(audioPath string) ([]byte, string, error) {
	f, err := os.Open(audioPath)
	if err != nil {
		return nil, "", fmt.Errorf("open audio: %w", err)
	}
	defer f.Close()

	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)

	part, err := w.CreateFormFile("file", filepath.Base(audioPath))
	if err != nil {
		return nil, "", fmt.Errorf("multipart file part: %w", err)
	}
	if _, err := io.Copy(part, f); err != nil {
		return nil, "", fmt.Errorf("copy audio into request: %w", err)
	}

	if err := w.WriteField("model", t.openAIModel()); err != nil {
		return nil, "", err
	}
	if err := w.WriteField("response_format", "json"); err != nil {
		return nil, "", err
	}
	// An explicit language is a hard constraint, not a hint: pinning "de" makes
	// an English voice note come back as garbled German. Omitting the field
	// lets the model detect per file, which is what a mixed-language inbox
	// needs, so "auto" and empty both mean "send nothing".
	if lang := normalizeWhisperLanguage(t.cfg.WhisperLanguage); lang != "" {
		if err := w.WriteField("language", lang); err != nil {
			return nil, "", err
		}
	}
	if err := w.Close(); err != nil {
		return nil, "", err
	}
	return buf.Bytes(), w.FormDataContentType(), nil
}

// openAIModel resolves the transcription model. WHATSAPP_WHISPER_MODEL is not
// reused here: its default is "large-v3", a whisper.cpp model name that the
// API rejects, so sharing the variable would break the default for whichever
// backend did not win.
func (t *Transcriber) openAIModel() string {
	if m := strings.TrimSpace(t.cfg.WhisperAPIModel); m != "" {
		return m
	}
	return "whisper-1"
}

func normalizeWhisperLanguage(lang string) string {
	l := strings.ToLower(strings.TrimSpace(lang))
	if l == "auto" || l == "detect" {
		return ""
	}
	return l
}

// postOpenAITranscription performs one attempt. The bool reports whether the
// failure is worth retrying.
func (t *Transcriber) postOpenAITranscription(ctx context.Context, body io.Reader, contentType string) (string, bool, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, openAITranscriptionURL, body)
	if err != nil {
		return "", false, err
	}
	req.Header.Set("Content-Type", contentType)
	req.Header.Set("Authorization", "Bearer "+t.cfg.WhisperAPIKey)

	resp, err := openAIHTTPClient.Do(req)
	if err != nil {
		// Network-level failure; the request may never have been served.
		return "", true, fmt.Errorf("openai request: %w", err)
	}
	defer resp.Body.Close()

	// Bounded read: an error body is small, and a transcript of a <=25 MB
	// audio file cannot plausibly exceed this.
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return "", true, fmt.Errorf("read openai response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		// 429 and 5xx are transient. 401/403 (bad key) and 400 (bad file,
		// unsupported format) are not — retrying those just burns the job's
		// remaining time and, on 429-adjacent quota errors, looks like abuse.
		retryable := resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500
		return "", retryable, fmt.Errorf("openai transcription HTTP %d: %s", resp.StatusCode, openAIErrorMessage(raw))
	}

	var parsed struct {
		Text string `json:"text"`
	}
	if err := json.Unmarshal(raw, &parsed); err != nil {
		return "", false, fmt.Errorf("parse openai response: %w", err)
	}
	return strings.TrimSpace(parsed.Text), false, nil
}

// openAIErrorMessage pulls the human-readable message out of OpenAI's error
// envelope, falling back to a truncated raw body. The API key is never part of
// a response body, but the output is truncated regardless so a large error page
// cannot flood the journal.
func openAIErrorMessage(raw []byte) string {
	var e struct {
		Error struct {
			Message string `json:"message"`
			Code    string `json:"code"`
		} `json:"error"`
	}
	if err := json.Unmarshal(raw, &e); err == nil && e.Error.Message != "" {
		if e.Error.Code != "" {
			return fmt.Sprintf("%s (%s)", e.Error.Message, e.Error.Code)
		}
		return e.Error.Message
	}
	s := strings.TrimSpace(string(raw))
	if len(s) > 300 {
		s = s[:300] + "…"
	}
	return s
}
