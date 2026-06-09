package handlers

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"time"

	"github.com/gofiber/fiber/v3"
	"github.com/jmoiron/sqlx"
)

// ── Summarize Handler ─────────────────────────────────────────────────────────

type SummarizeHandler struct {
	db       *sqlx.DB
	endpoint string
	apiKey   string
	model    string
}

func NewSummarizeHandler(db *sqlx.DB, endpoint, apiKey, model string) *SummarizeHandler {
	return &SummarizeHandler{db: db, endpoint: endpoint, apiKey: apiKey, model: model}
}

// POST /api/v1/spikes/:spike_id/summarize — generate context summary via LLM
func (h *SummarizeHandler) SummarizeSpike(c fiber.Ctx) error {
	spikeID, err := strconv.Atoi(c.Params("spike_id"))
	if err != nil {
		return c.Status(400).JSON(fiber.Map{"error": "invalid spike id"})
	}
	// Load spike + stream info
	type SpikeRow struct {
		ID              int     `db:"id"`
		StreamID        int     `db:"stream_id"`
		StreamTimestamp string  `db:"stream_timestamp"`
		StreamSeconds   float64 `db:"stream_seconds"`
		ContextSummary  *string `db:"context_summary"`
	}
	var spike SpikeRow
	err = h.db.Get(&spike,
		`SELECT sr.id, sr.stream_id, sr.stream_timestamp, sr.stream_seconds, sr.context_summary
		 FROM spike_results sr WHERE sr.id = $1`, spikeID)
	if err != nil {
		return c.Status(404).JSON(fiber.Map{"error": "spike not found"})
	}

	// Return cached result if already summarized
	if spike.ContextSummary != nil && *spike.ContextSummary != "" {
		return c.JSON(fiber.Map{
			"spike_id":        spike.ID,
			"context_summary": *spike.ContextSummary,
			"cached":          true,
		})
	}

	// Load stream title for the prompt
	var streamTitle string
	if err := h.db.Get(&streamTitle,
		"SELECT COALESCE(title, 'Unknown Stream') FROM streams WHERE id = $1",
		spike.StreamID); err != nil {
		streamTitle = "Unknown Stream"
	}

	// Fetch chat messages in a window around the spike
	type ChatMsg struct {
		Author  string `db:"author"`
		Message string `db:"message"`
	}
	var messages []ChatMsg
	halfWindow := 30.0
	if err := h.db.Select(&messages,
		`SELECT author, message FROM chat_messages
		 WHERE stream_id = $1
		   AND time_in_seconds >= $2
		   AND time_in_seconds <= $3
		 ORDER BY time_in_seconds ASC
		 LIMIT 100`,
		spike.StreamID,
		spike.StreamSeconds-halfWindow,
		spike.StreamSeconds+halfWindow,
	); err != nil {
		return c.Status(500).JSON(fiber.Map{"error": "failed to fetch chat messages"})
	}

	if len(messages) == 0 {
		return c.Status(404).JSON(fiber.Map{"error": "no chat messages found near this spike"})
	}

	// Build chat text for the prompt
	var chatLines string
	for _, m := range messages {
		if m.Message != "" {
			chatLines += fmt.Sprintf("%s: %s\n", m.Author, m.Message)
		}
	}

	if chatLines == "" {
		return c.Status(404).JSON(fiber.Map{"error": "no chat message content to summarize"})
	}

	// Call LLM
	summary, err := callSummarizer(h.endpoint, h.apiKey, h.model, streamTitle, spike.StreamTimestamp, chatLines)
	if err != nil {
		return c.Status(502).JSON(fiber.Map{"error": "summarizer call failed: " + err.Error()})
	}

	// Save to DB
	if _, err := h.db.Exec("UPDATE spike_results SET context_summary = $1 WHERE id = $2", summary, spikeID); err != nil {
		fmt.Printf("WARN: failed to cache summary for spike %d: %v\n", spikeID, err)
	}

	return c.JSON(fiber.Map{
		"spike_id":        spike.ID,
		"context_summary": summary,
		"cached":          false,
	})
}

// callSummarizer sends chat messages to an OpenAI-compatible endpoint.
func callSummarizer(endpoint, apiKey, model, streamTitle, spikeTimestamp, chatText string) (string, error) {
	systemPrompt := "You are analyzing a YouTube livestream chat. A spike in message rate was detected. " +
		"Summarize what happened in the stream that caused viewers to spam chat. " +
		"Respond in 1–2 sentences in English. Be specific: mention names, events, or reactions if visible. " +
		"Do NOT say 'the chat spiked because...' — just state what happened directly. " +
		"If messages look like bot spam or a viewer raid, say 'Appears to be bot spam or a viewer raid.'"

	userPrompt := fmt.Sprintf("Stream: %s\nSpike occurred at: %s\nChat messages in the spike window:\n%s",
		streamTitle, spikeTimestamp, chatText)

	payload := map[string]interface{}{
		"model": model,
		"messages": []map[string]string{
			{"role": "system", "content": systemPrompt},
			{"role": "user", "content": userPrompt},
		},
		"max_tokens":  400,
		"temperature": 0.3,
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return "", fmt.Errorf("marshal payload: %w", err)
	}

	req, err := http.NewRequest("POST", endpoint, bytes.NewReader(body))
	if err != nil {
		return "", fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+apiKey)
	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("http request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return "", fmt.Errorf("llm returned %d: %s", resp.StatusCode, string(respBody))
	}

	var result struct {
		Choices []struct {
			Message struct {
				Content          string `json:"content"`
				ReasoningContent string `json:"reasoning_content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", fmt.Errorf("decode llm response: %w", err)
	}
	if len(result.Choices) == 0 {
		return "", fmt.Errorf("llm returned no choices")
	}

	content := result.Choices[0].Message.Content
	if content == "" {
		// DeepSeek thinking models put the answer in reasoning_content when
		// max_tokens is too low to finish the response — use it as fallback
		content = result.Choices[0].Message.ReasoningContent
	}
	if content == "" {
		return "", fmt.Errorf("llm returned empty content")
	}
	return content, nil
}

// POST /api/v1/streams/:id/summarize-all — summarize all unsummarized spikes for a stream
func (h *SummarizeHandler) SummarizeAll(c fiber.Ctx) error {
	streamID, err := strconv.Atoi(c.Params("id"))
	if err != nil {
		return c.Status(400).JSON(fiber.Map{"error": "invalid stream id"})
	}

	// Load stream title
	var streamTitle string
	if err := h.db.Get(&streamTitle,
		"SELECT COALESCE(title, 'Unknown Stream') FROM streams WHERE id = $1", streamID); err != nil {
		return c.Status(404).JSON(fiber.Map{"error": "stream not found"})
	}

	// Fetch all spikes for this stream that don't have a summary yet
	type SpikeRow struct {
		ID              int     `db:"id"`
		StreamTimestamp string  `db:"stream_timestamp"`
		StreamSeconds   float64 `db:"stream_seconds"`
		ContextSummary  *string `db:"context_summary"`
	}
	var spikes []SpikeRow
	if err := h.db.Select(&spikes,
		`SELECT id, stream_timestamp, stream_seconds, context_summary
		 FROM spike_results
		 WHERE stream_id = $1
		 ORDER BY stream_seconds ASC`, streamID); err != nil {
		return c.Status(500).JSON(fiber.Map{"error": "failed to fetch spikes"})
	}
	if len(spikes) == 0 {
		return c.Status(404).JSON(fiber.Map{"error": "no spikes found for this stream"})
	}

	type SpikeResult struct {
		SpikeID        int    `json:"spike_id"`
		Summary        string `json:"summary"`
		Cached         bool   `json:"cached"`
		Error          string `json:"error,omitempty"`
	}

	results := make([]SpikeResult, 0, len(spikes))
	summarized := 0
	cached := 0
	failed := 0

	for _, spike := range spikes {
		// Return cached result if already summarized
		if spike.ContextSummary != nil && *spike.ContextSummary != "" {
			results = append(results, SpikeResult{
				SpikeID: spike.ID,
				Summary: *spike.ContextSummary,
				Cached:  true,
			})
			cached++
			continue
		}

		// Fetch chat messages in window around spike
		type ChatMsg struct {
			Author  string `db:"author"`
			Message string `db:"message"`
		}
		var messages []ChatMsg
		halfWindow := 30.0
		if err := h.db.Select(&messages,
			`SELECT author, message FROM chat_messages
			 WHERE stream_id = $1
			   AND time_in_seconds >= $2
			   AND time_in_seconds <= $3
			 ORDER BY time_in_seconds ASC
			 LIMIT 100`,
			streamID,
			spike.StreamSeconds-halfWindow,
			spike.StreamSeconds+halfWindow,
		); err != nil {
			results = append(results, SpikeResult{SpikeID: spike.ID, Error: "db error fetching messages"})
			failed++
			continue
		}
		if len(messages) == 0 {
			results = append(results, SpikeResult{SpikeID: spike.ID, Error: "no chat messages near this spike"})
			failed++
			continue
		}

		// Build chat text
		var chatLines string
		for _, m := range messages {
			if m.Message != "" {
				chatLines += fmt.Sprintf("%s: %s\n", m.Author, m.Message)
			}
		}
		if chatLines == "" {
			results = append(results, SpikeResult{SpikeID: spike.ID, Error: "no message content"})
			failed++
			continue
		}

		// Call LLM
		summary, err := callSummarizer(h.endpoint, h.apiKey, h.model, streamTitle, spike.StreamTimestamp, chatLines)
		if err != nil {
			results = append(results, SpikeResult{SpikeID: spike.ID, Error: err.Error()})
			failed++
			continue
		}

		// Cache in DB
		if _, err := h.db.Exec("UPDATE spike_results SET context_summary = $1 WHERE id = $2", summary, spike.ID); err != nil {
			fmt.Printf("WARN: failed to cache summary for spike %d: %v\n", spike.ID, err)
		}

		results = append(results, SpikeResult{SpikeID: spike.ID, Summary: summary, Cached: false})
		summarized++
	}

	return c.JSON(fiber.Map{
		"stream_id":   streamID,
		"total":       len(spikes),
		"summarized":  summarized,
		"cached":      cached,
		"failed":      failed,
		"results":     results,
	})
}

// GET /api/v1/spikes/:spike_id — get a single spike with its summary
func (h *SummarizeHandler) GetSpike(c fiber.Ctx) error {
	spikeID, err := strconv.Atoi(c.Params("spike_id"))
	if err != nil {
		return c.Status(400).JSON(fiber.Map{"error": "invalid spike id"})
	}

	type SpikeDetail struct {
		ID                 int      `db:"id"                   json:"id"`
		StreamID           int      `db:"stream_id"            json:"stream_id"`
		StreamTimestamp    string   `db:"stream_timestamp"     json:"stream_timestamp"`
		StreamSeconds      float64  `db:"stream_seconds"       json:"stream_seconds"`
		CurrentRatePerMin  float64  `db:"current_rate_per_min" json:"current_rate_per_min"`
		BaselineRatePerMin float64  `db:"baseline_rate_per_min" json:"baseline_rate_per_min"`
		SpikeRatio         float64  `db:"spike_ratio"          json:"spike_ratio"`
		MessagesInWindow   int      `db:"messages_in_window"   json:"messages_in_window"`
		ContextSummary     *string  `db:"context_summary"      json:"context_summary"`
		DetectedAt         string   `db:"detected_at"          json:"detected_at"`
	}

	var row SpikeDetail
	if err := h.db.Get(&row, "SELECT * FROM spike_results WHERE id = $1", spikeID); err != nil {
		return c.Status(404).JSON(fiber.Map{"error": "spike not found"})
	}
	return c.JSON(row)
}
