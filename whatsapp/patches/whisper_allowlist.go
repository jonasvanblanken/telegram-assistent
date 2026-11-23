// whisper_allowlist.go — Allow-Liste fuer die Transkription.
//
// LOCAL PATCH, not upstream. Upstream offers only WHATSAPP_WHISPER_EXCLUDE_CHATS,
// a deny-list. A deny-list has the wrong default for an unattended automation:
// every NEW chat is transcribed until someone remembers to add it. For a system
// whose scope is "the people on the Freunde list", the correct default is the
// other way round — transcribe nothing unless the chat is named.
//
// WHATSAPP_WHISPER_ONLY_CHATS holds the same kind of patterns as the exclude
// list (normalized, accent-folded substrings matched against chat JID and chat
// name). Empty means the allow-list is off and behaviour is exactly upstream's.
//
// Deny still wins: a chat on both lists is not transcribed. That ordering is
// deliberate — the exclude list is the stronger, privacy-protecting statement,
// and a conflict between the two should resolve toward not sending audio.

package main

import (
	"context"
	"database/sql"
	"errors"
	"log"
	"strings"
)

// chatAllowed reports whether this chat may be transcribed at all.
//
// Fails CLOSED, mirroring chatExcluded: if the chat row cannot be read we
// cannot prove the chat is on the allow-list, and an unprovable chat must not
// have its audio sent to a third party. A missed transcript is recoverable —
// the backfill sweeper retries it for 14 days. Audio sent to OpenAI is not.
// sql.ErrNoRows is NOT an error here: a chat row may legitimately not exist
// yet, and the JID itself is still matched below.
func (t *Transcriber) chatAllowed(chatJID string) bool {
	if len(t.cfg.WhisperOnlyChats) == 0 {
		return true // allow-list off — upstream behaviour
	}
	if chatJID == "" {
		return false
	}
	// Match against EVERY identity WhatsApp knows for this person, not just the
	// JID the message happened to arrive under. A contact's traffic migrates
	// between the phone-number JID and a @lid without warning — this store
	// already has 62 direct chats in LID form. An allow-list entry written as
	// one JID would silently stop matching the day the other form takes over,
	// and because this filter fails closed that shows up as "their voice notes
	// quietly stopped being transcribed", not as an error. handleListMessages
	// resolves aliases for exactly the same reason.
	// On an alias-resolution error, fall back to the JID we were given rather
	// than blocking outright. Checking fewer identities can only ever match
	// FEWER chats, never more, so this cannot leak — while blocking outright
	// would turn an unrelated database hiccup into "transcription silently
	// stopped for everyone". handleListMessages degrades the same way.
	jids, err := resolveAliases(context.Background(), t.db, chatJID)
	if err != nil {
		log.Printf("transcriber: alias resolve for %s failed (%v); checking the given JID only", chatJID, err)
		jids = []string{chatJID}
	}

	for _, jid := range jids {
		var name, normName string
		err := t.db.QueryRow(`SELECT COALESCE(name,''), COALESCE(normalized_name,'') FROM chats WHERE jid = ?`, jid).Scan(&name, &normName)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			log.Printf("transcriber: chat lookup for %s failed (%v); treating as NOT ALLOWED so audio is never sent on a lookup error", jid, err)
			return false
		}
		hay := strings.ToLower(jid + " " + Normalize(name) + " " + normName)
		for _, pat := range t.cfg.WhisperOnlyChats {
			if strings.Contains(hay, pat) {
				return true
			}
		}
	}
	return false
}
