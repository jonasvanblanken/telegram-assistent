// delta.go — das gesamte Delta seit einem Zeitpunkt in EINEM Aufruf.
//
// LOCAL PATCH, not upstream.
//
// Upstream kennt nur `GET /api/messages?chat_jid=…`: ein Aufruf pro Chat. Ein
// nächtlicher Sweep über eine aktive Chatliste braucht damit `list_chats` plus
// ein `list_messages` je Kandidat — in der Praxis 30 bis 50 sequenzielle
// MCP-Runden. In der Nacht auf den 2026-09-02 ist der Lauf genau daran
// gestorben: die Agent-Schleife wurde beendet, bevor irgendetwas geschrieben
// war, und hinterließ nicht einmal einen Fehler-Report.
//
// Dieser Endpunkt liefert stattdessen alle Nachrichten seit `since`, nach Chat
// gruppiert, in einem Rutsch. Der Konsument macht danach keinen weiteren
// Tool-Call mehr, sondern nur noch Vault-Schreibvorgänge.
//
// Semantik der Obergrenze: zurückgegeben werden die **neuesten** `limit`
// Nachrichten. Wird gekürzt, steht `truncated: true` und `oldest_returned` auf
// dem ältesten enthaltenen Zeitstempel. Der Aufrufer darf seinen Watermark dann
// **nicht** über diesen Wert hinaus setzen, sonst überspringt er die Lücke
// dauerhaft.

package main

import (
	"fmt"
	"net/http"
	"strconv"
)

const (
	deltaDefaultLimit = 400
	deltaMaxLimit     = 1500
)

type deltaChat struct {
	JID      string       `json:"jid"`
	Name     string       `json:"name"`
	ChatType string       `json:"chat_type"`
	Messages []messageRow `json:"messages"`
}

type deltaResponse struct {
	Since          int64             `json:"since"`
	Count          int               `json:"count"`
	ChatCount      int               `json:"chat_count"`
	Truncated      bool              `json:"truncated"`
	OldestReturned int64             `json:"oldest_returned,omitempty"`
	Chats          []deltaChat       `json:"chats"`
	BridgeState    *bridgeStateBrief `json:"_bridge_state,omitempty"`
}

// handleMessagesSince serves GET /api/messages/since?since=<unix>&limit=<n>.
func (s *Server) handleMessagesSince(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	since, _ := strconv.ParseInt(q.Get("since"), 10, 64)
	if since <= 0 {
		writeJSON(w, http.StatusBadRequest, errorResponse{
			Error:   "since required",
			Details: "unix timestamp in seconds; use the watermark of the last successful sweep",
		})
		return
	}
	limit := parseIntDefault(q.Get("limit"), deltaDefaultLimit)
	if limit > deltaMaxLimit {
		limit = deltaMaxLimit
	}
	if limit < 1 {
		limit = 1
	}

	// Newest-first with the cap applied across ALL chats, so a single very
	// chatty group cannot crowd out everyone else's newer messages.
	rows, err := s.db.QueryContext(r.Context(), `
		SELECT m.id, m.chat_jid, COALESCE(m.sender_jid,''), COALESCE(m.sender_display,''),
		       m.timestamp, m.type,
		       COALESCE(m.scrubbed_text, COALESCE(m.content_text,'')),
		       m.is_from_me, m.voice_note_transcript,
		       COALESCE(c.name,''), COALESCE(c.chat_type,'')
		FROM messages m
		LEFT JOIN chats c ON c.jid = m.chat_jid
		WHERE m.timestamp > ?
		ORDER BY m.timestamp DESC, m.id DESC
		LIMIT ?
	`, since, limit+1) // +1 nur zum Erkennen, ob gekürzt wurde
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, errorResponse{Error: "query failed", Details: err.Error()})
		return
	}
	defer rows.Close()

	type collected struct {
		msg      messageRow
		chatName string
		chatType string
	}
	all := make([]collected, 0, limit)
	for rows.Next() {
		var c collected
		var fromMe int
		if err := rows.Scan(&c.msg.ID, &c.msg.ChatJID, &c.msg.SenderJID, &c.msg.SenderDisplay,
			&c.msg.Timestamp, &c.msg.Type, &c.msg.ContentText, &fromMe, &c.msg.Transcript,
			&c.chatName, &c.chatType); err != nil {
			continue
		}
		c.msg.IsFromMe = fromMe == 1
		all = append(all, c)
	}

	truncated := len(all) > limit
	if truncated {
		all = all[:limit]
	}

	// Nach Chat gruppieren, Reihenfolge der Chats = Reihenfolge des ersten
	// (also neuesten) Treffers. Innerhalb eines Chats bleibt es neueste zuerst.
	idx := map[string]int{}
	out := deltaResponse{Since: since, Truncated: truncated, Chats: []deltaChat{}}
	var oldest int64
	for _, c := range all {
		i, ok := idx[c.msg.ChatJID]
		if !ok {
			name := c.chatName
			if name == "" {
				name = fmt.Sprintf("(kein Name) %s", c.msg.ChatJID)
			}
			out.Chats = append(out.Chats, deltaChat{
				JID: c.msg.ChatJID, Name: name, ChatType: c.chatType, Messages: []messageRow{},
			})
			i = len(out.Chats) - 1
			idx[c.msg.ChatJID] = i
		}
		out.Chats[i].Messages = append(out.Chats[i].Messages, c.msg)
		if oldest == 0 || c.msg.Timestamp < oldest {
			oldest = c.msg.Timestamp
		}
	}
	out.Count = len(all)
	out.ChatCount = len(out.Chats)
	if truncated {
		out.OldestReturned = oldest
	}
	// Guard statt blindem Aufruf: currentBridgeState dereferenziert s.bridge,
	// und ein Server ohne Bridge ist ein legitimer Zustand (Tests, und der
	// kurze Moment vor dem Verbindungsaufbau).
	if s.bridge != nil {
		out.BridgeState = s.currentBridgeState()
	}
	writeJSON(w, http.StatusOK, out)
}
