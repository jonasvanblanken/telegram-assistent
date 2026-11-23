package main

import (
	"database/sql"
	"testing"

	_ "github.com/mutecomm/go-sqlcipher/v4"
)

func allowlistDB(t *testing.T, name string) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite3", "file:"+name+"?mode=memory&cache=shared")
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	if _, err := db.Exec(`CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, normalized_name TEXT)`); err != nil {
		t.Fatalf("create chats: %v", err)
	}
	// Mirrors migration 003. chatAllowed resolves aliases, so the fixture has
	// to carry this table or every case would exercise the degraded path.
	if _, err := db.Exec(`CREATE TABLE jid_aliases (jid_a TEXT, jid_b TEXT)`); err != nil {
		t.Fatalf("create jid_aliases: %v", err)
	}
	return db
}

// Empty allow-list must behave exactly like upstream: everything passes.
// Otherwise enabling the patch would silently switch transcription off for an
// existing install that never set the variable.
func TestChatAllowed_EmptyListAllowsEverything(t *testing.T) {
	tr := &Transcriber{cfg: &Config{}, db: allowlistDB(t, "allow_empty")}
	if !tr.chatAllowed("999@s.whatsapp.net") {
		t.Fatal("an unset allow-list must not block anything")
	}
}

func TestChatAllowed_MatchesOnChatName(t *testing.T) {
	db := allowlistDB(t, "allow_name")
	if _, err := db.Exec(`INSERT INTO chats VALUES ('111@s.whatsapp.net','Anna Beispiel','anna beispiel')`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO chats VALUES ('222@s.whatsapp.net','Mira','mira')`); err != nil {
		t.Fatal(err)
	}
	tr := &Transcriber{cfg: &Config{WhisperOnlyChats: []string{"anna beispiel"}}, db: db}

	if !tr.chatAllowed("111@s.whatsapp.net") {
		t.Error("a chat named on the allow-list must be transcribed")
	}
	if tr.chatAllowed("222@s.whatsapp.net") {
		t.Error("a chat NOT on the allow-list must not be transcribed")
	}
}

// The whole point of an allow-list over a deny-list: a chat nobody has heard of
// yet is silent by default instead of being transcribed until someone notices.
func TestChatAllowed_UnknownChatIsBlockedByDefault(t *testing.T) {
	tr := &Transcriber{cfg: &Config{WhisperOnlyChats: []string{"anna beispiel"}}, db: allowlistDB(t, "allow_new")}
	if tr.chatAllowed("999@s.whatsapp.net") {
		t.Fatal("a brand-new chat must NOT be transcribed while an allow-list is active")
	}
}

// Mirrors TestChatExcluded_FailsClosedOnLookupError. A missed transcript is
// recoverable (the sweeper retries for 14 days); audio sent to OpenAI is not.
func TestChatAllowed_FailsClosedOnLookupError(t *testing.T) {
	db, err := sql.Open("sqlite3", "file:allow_failclosed?mode=memory&cache=shared")
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	_ = db.Close() // every query now errors

	tr := &Transcriber{cfg: &Config{WhisperOnlyChats: []string{"anna"}}, db: db}
	if tr.chatAllowed("111@s.whatsapp.net") {
		t.Fatal("allow-list failed OPEN on a lookup error: audio would be sent for an unprovable chat")
	}
}

// A missing chats row is not an error — the JID itself still matches.
func TestChatAllowed_MissingRowStillMatchesJID(t *testing.T) {
	tr := &Transcriber{cfg: &Config{WhisperOnlyChats: []string{"4915100000004"}}, db: allowlistDB(t, "allow_norow")}
	if !tr.chatAllowed("4915100000004@s.whatsapp.net") {
		t.Fatal("a JID-matching pattern must allow even when the chats row is absent")
	}
}

// Deny beats allow. The exclude list is the stronger privacy statement, so a
// chat on both must not be transcribed.
func TestAllowAndExclude_DenyWins(t *testing.T) {
	db := allowlistDB(t, "allow_denywins")
	if _, err := db.Exec(`INSERT INTO chats VALUES ('111@s.whatsapp.net','Anna Beispiel','anna beispiel')`); err != nil {
		t.Fatal(err)
	}
	tr := &Transcriber{cfg: &Config{
		WhisperOnlyChats:    []string{"anna beispiel"},
		WhisperExcludeChats: []string{"anna beispiel"},
	}, db: db}

	if !tr.chatAllowed("111@s.whatsapp.net") {
		t.Fatal("precondition: the chat is on the allow-list")
	}
	if !tr.chatExcluded("111@s.whatsapp.net") {
		t.Fatal("a chat on both lists must still be excluded — deny wins")
	}
}

// Accent folding has to work the same way it does for the exclude list, or an
// allow-list entry typed without umlauts would silently drop a real person.
func TestChatAllowed_AccentInsensitive(t *testing.T) {
	db := allowlistDB(t, "allow_accents")
	if _, err := db.Exec(`INSERT INTO chats VALUES ('333@s.whatsapp.net','Paul Öllers','paul ollers')`); err != nil {
		t.Fatal(err)
	}
	tr := &Transcriber{cfg: &Config{WhisperOnlyChats: splitNormalizedCSV("Paul Öllers")}, db: db}
	if !tr.chatAllowed("333@s.whatsapp.net") {
		t.Fatal("allow-list matching must fold accents like the exclude list does")
	}
}

// A contact whose traffic has moved to a @lid must still match an allow-list
// entry written as their phone JID. Without alias resolution this fails closed
// and their voice notes silently stop being transcribed — a failure that looks
// exactly like "they sent nothing".
func TestChatAllowed_MatchesThroughLIDAlias(t *testing.T) {
	db := allowlistDB(t, "allow_alias")
	// The chat row exists only under the LID form, with no name yet.
	if _, err := db.Exec(`INSERT INTO chats VALUES ('22000000000000@lid','','')`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO jid_aliases VALUES ('22000000000000@lid','4915100000004@s.whatsapp.net')`); err != nil {
		t.Fatal(err)
	}

	tr := &Transcriber{cfg: &Config{WhisperOnlyChats: []string{"4915100000004@s.whatsapp.net"}}, db: db}
	if !tr.chatAllowed("22000000000000@lid") {
		t.Fatal("allow-list entry written as the phone JID must still match once the chat moved to a LID")
	}
}

// Alias resolution must not turn the allow-list into a sieve: an unrelated
// contact with aliases of their own still has to be blocked.
func TestChatAllowed_AliasResolutionDoesNotLeak(t *testing.T) {
	db := allowlistDB(t, "allow_alias_leak")
	if _, err := db.Exec(`INSERT INTO chats VALUES ('110000000000000@lid','Mira','mira')`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO jid_aliases VALUES ('110000000000000@lid','4915100000003@s.whatsapp.net')`); err != nil {
		t.Fatal(err)
	}

	tr := &Transcriber{cfg: &Config{WhisperOnlyChats: []string{"4915100000004@s.whatsapp.net"}}, db: db}
	if tr.chatAllowed("110000000000000@lid") {
		t.Fatal("a contact who is not on the allow-list must stay blocked even when they have aliases")
	}
}

// A broken jid_aliases table must not switch transcription off for everyone.
// Degrading to the given JID can only match fewer chats, so it stays safe.
func TestChatAllowed_DegradesGracefullyWithoutAliasTable(t *testing.T) {
	db, err := sql.Open("sqlite3", "file:allow_noaliastable?mode=memory&cache=shared")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	if _, err := db.Exec(`CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, normalized_name TEXT)`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO chats VALUES ('111@s.whatsapp.net','Anna Beispiel','anna beispiel')`); err != nil {
		t.Fatal(err)
	}
	// No jid_aliases table at all -> resolveAliases errors.
	tr := &Transcriber{cfg: &Config{WhisperOnlyChats: []string{"anna beispiel"}}, db: db}
	if !tr.chatAllowed("111@s.whatsapp.net") {
		t.Error("a missing alias table must not block a chat that is plainly on the list")
	}
	tr2 := &Transcriber{cfg: &Config{WhisperOnlyChats: []string{"someone else"}}, db: db}
	if tr2.chatAllowed("111@s.whatsapp.net") {
		t.Error("the degraded path must still block a chat that is not on the list")
	}
}
