package main

import (
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	_ "github.com/mutecomm/go-sqlcipher/v4"
)

func deltaTestServer(t *testing.T, name string) *Server {
	t.Helper()
	db, err := sql.Open("sqlite3", "file:"+name+"?mode=memory&cache=shared")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	// Spiegelt die echten Spalten aus migrations/001_initial.sql, soweit dieser
	// Endpunkt sie liest. Eine abweichende Fixture wuerde einen Schemafehler
	// verstecken statt ihn zu zeigen.
	if _, err := db.Exec(`CREATE TABLE chats (
		jid TEXT PRIMARY KEY, name TEXT, normalized_name TEXT, chat_type TEXT)`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`CREATE TABLE messages (
		id TEXT PRIMARY KEY, chat_jid TEXT, sender_jid TEXT, sender_display TEXT,
		timestamp INTEGER, type TEXT, content_text TEXT, scrubbed_text TEXT,
		is_from_me INTEGER, voice_note_transcript TEXT)`); err != nil {
		t.Fatal(err)
	}
	return &Server{db: db, cfg: &Config{}}
}

func addMsg(t *testing.T, s *Server, id, chat string, ts int64, fromMe int) {
	t.Helper()
	if _, err := s.db.Exec(`INSERT INTO messages VALUES (?,?,?,?,?,?,?,NULL,?,NULL)`,
		id, chat, chat, "", ts, "text", "hallo", fromMe); err != nil {
		t.Fatal(err)
	}
}

func callDelta(t *testing.T, s *Server, query string) deltaResponse {
	t.Helper()
	rec := httptest.NewRecorder()
	s.handleMessagesSince(rec, httptest.NewRequest(http.MethodGet, "/api/messages/since?"+query, nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	var out deltaResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	return out
}

// Der eigentliche Zweck: ein Aufruf liefert mehrere Chats gruppiert.
func TestDelta_GroupsAllChatsInOneCall(t *testing.T) {
	s := deltaTestServer(t, "delta_group")
	if _, err := s.db.Exec(`INSERT INTO chats VALUES ('a@s.whatsapp.net','Anna Beispiel','anna beispiel','direct')`); err != nil {
		t.Fatal(err)
	}
	if _, err := s.db.Exec(`INSERT INTO chats VALUES ('b@s.whatsapp.net','Timo','timo','direct')`); err != nil {
		t.Fatal(err)
	}
	addMsg(t, s, "m1", "a@s.whatsapp.net", 100, 0)
	addMsg(t, s, "m2", "a@s.whatsapp.net", 300, 1)
	addMsg(t, s, "m3", "b@s.whatsapp.net", 200, 0)

	out := callDelta(t, s, "since=50")
	if out.Count != 3 || out.ChatCount != 2 {
		t.Fatalf("count=%d chat_count=%d, want 3/2", out.Count, out.ChatCount)
	}
	// Chat mit der neuesten Nachricht zuerst.
	if out.Chats[0].Name != "Anna Beispiel" {
		t.Errorf("erster Chat = %q, erwartet den mit der juengsten Nachricht", out.Chats[0].Name)
	}
	// Innerhalb eines Chats neueste zuerst.
	if out.Chats[0].Messages[0].ID != "m2" {
		t.Errorf("Reihenfolge im Chat falsch: %s", out.Chats[0].Messages[0].ID)
	}
}

// `since` ist exklusiv — die Nachricht, die den Watermark gesetzt hat, darf
// nicht jede Nacht erneut auftauchen.
func TestDelta_SinceIsExclusive(t *testing.T) {
	s := deltaTestServer(t, "delta_excl")
	if _, err := s.db.Exec(`INSERT INTO chats VALUES ('a@s.whatsapp.net','A','a','direct')`); err != nil {
		t.Fatal(err)
	}
	addMsg(t, s, "m1", "a@s.whatsapp.net", 100, 0)
	addMsg(t, s, "m2", "a@s.whatsapp.net", 200, 0)

	out := callDelta(t, s, "since=100")
	if out.Count != 1 || out.Chats[0].Messages[0].ID != "m2" {
		t.Fatalf("since muss exklusiv sein, bekam count=%d", out.Count)
	}
}

// Ohne `since` darf nicht stillschweigend die ganze Historie zurueckkommen.
func TestDelta_RequiresSince(t *testing.T) {
	s := deltaTestServer(t, "delta_nosince")
	rec := httptest.NewRecorder()
	s.handleMessagesSince(rec, httptest.NewRequest(http.MethodGet, "/api/messages/since", nil))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status %d, want 400", rec.Code)
	}
}

// Die Obergrenze greift ueber ALLE Chats, damit ein einzelner vielschreibender
// Chat nicht die neueren Nachrichten aller anderen verdraengt. Bei Kuerzung
// muss `oldest_returned` gesetzt sein, sonst kann der Aufrufer seinen Watermark
// nicht sicher setzen.
func TestDelta_TruncationReportsOldest(t *testing.T) {
	s := deltaTestServer(t, "delta_trunc")
	if _, err := s.db.Exec(`INSERT INTO chats VALUES ('a@s.whatsapp.net','A','a','direct')`); err != nil {
		t.Fatal(err)
	}
	for i := 1; i <= 10; i++ {
		addMsg(t, s, string(rune('a'+i))+"x", "a@s.whatsapp.net", int64(100+i), 0)
	}
	out := callDelta(t, s, "since=50&limit=4")
	if !out.Truncated {
		t.Fatal("truncated muss true sein")
	}
	if out.Count != 4 {
		t.Errorf("count=%d, want 4", out.Count)
	}
	if out.OldestReturned != 107 {
		t.Errorf("oldest_returned=%d, want 107 (die aeltesten der 4 neuesten)", out.OldestReturned)
	}
}

func TestDelta_NoTruncationFlagWhenComplete(t *testing.T) {
	s := deltaTestServer(t, "delta_notrunc")
	if _, err := s.db.Exec(`INSERT INTO chats VALUES ('a@s.whatsapp.net','A','a','direct')`); err != nil {
		t.Fatal(err)
	}
	addMsg(t, s, "m1", "a@s.whatsapp.net", 100, 0)
	out := callDelta(t, s, "since=50&limit=10")
	if out.Truncated || out.OldestReturned != 0 {
		t.Fatalf("vollstaendige Antwort darf nicht als gekuerzt markiert sein")
	}
}
