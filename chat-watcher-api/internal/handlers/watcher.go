package handlers

import (
	"bytes"
	"fmt"
	"os/exec"
	"path/filepath"
	"strconv"

	"github.com/gofiber/fiber/v3"
	"github.com/jmoiron/sqlx"
)

type WatcherHandler struct {
	db         *sqlx.DB
	botPath    string
	pythonPath string
}

func NewWatcherHandler(db *sqlx.DB, botPath, pythonPath string) *WatcherHandler {
	return &WatcherHandler{db: db, botPath: botPath, pythonPath: pythonPath}
}

// POST /api/v1/streams/:id/run — spawn the bot watcher for a stream
func (h *WatcherHandler) RunStream(c fiber.Ctx) error {
	streamID, err := strconv.Atoi(c.Params("id"))
	if err != nil {
		return c.Status(400).JSON(fiber.Map{"error": "invalid stream id"})
	}

	// Load stream
	type StreamRow struct {
		ID      int    `db:"id"`
		VideoID string `db:"video_id"`
		Status  string `db:"status"`
	}
	var stream StreamRow
	if err := h.db.Get(&stream,
		"SELECT id, video_id, status FROM streams WHERE id = $1", streamID); err != nil {
		return c.Status(404).JSON(fiber.Map{"error": "stream not found"})
	}

	// Mark stream as pending so the bot picks it up
	h.db.Exec("UPDATE streams SET status='pending', error_message=NULL WHERE id=$1", streamID)

	// Spawn bot: python main.py run <video_id>
	var stderr bytes.Buffer
	cmd := exec.Command(h.pythonPath, h.botPath, "run", stream.VideoID)
	cmd.Dir = filepath.Dir(h.botPath)
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		// Revert status on failure
		h.db.Exec("UPDATE streams SET status=$1 WHERE id=$2", stream.Status, streamID)
		return c.Status(500).JSON(fiber.Map{"error": "failed to start watcher: " + err.Error()})
	}
	go func() {
		err := cmd.Wait()
		if err != nil {
			errStr := err.Error()
			if stderr.Len() > 0 {
				errStr = fmt.Sprintf("%s — %s", err.Error(), stderr.String())
			}
			// Clip error message to avoid DB column overflow (last 2000 chars)
			if len(errStr) > 2000 {
				errStr = errStr[len(errStr)-2000:]
			}
			h.db.Exec("UPDATE streams SET status='error', error_message=$1 WHERE id=$2 AND status NOT IN ('done','error')",
				errStr, streamID)
		}
	}()

	return c.Status(202).JSON(fiber.Map{
		"stream_id": streamID,
		"video_id":  stream.VideoID,
		"status":    "pending",
		"message":   "watcher started",
	})
}

// POST /api/v1/streams/:id/rerun — delete existing spikes then re-run the watcher
func (h *WatcherHandler) RerunStream(c fiber.Ctx) error {
	streamID, err := strconv.Atoi(c.Params("id"))
	if err != nil {
		return c.Status(400).JSON(fiber.Map{"error": "invalid stream id"})
	}

	// Load stream
	type StreamRow struct {
		ID      int    `db:"id"`
		VideoID string `db:"video_id"`
		Status  string `db:"status"`
	}
	var stream StreamRow
	if err := h.db.Get(&stream,
		"SELECT id, video_id, status FROM streams WHERE id = $1", streamID); err != nil {
		return c.Status(404).JSON(fiber.Map{"error": "stream not found"})
	}

	// Delete all existing spike results for this stream
	if _, err := h.db.Exec("DELETE FROM spike_results WHERE stream_id = $1", streamID); err != nil {
		return c.Status(500).JSON(fiber.Map{"error": "failed to clear spike results: " + err.Error()})
	}

	// Reset stream status to pending and clear error message
	h.db.Exec("UPDATE streams SET status='pending', error_message=NULL WHERE id=$1", streamID)

	// Spawn bot: python main.py run <video_id>
	var stderr bytes.Buffer
	cmd := exec.Command(h.pythonPath, h.botPath, "run", stream.VideoID)
	cmd.Dir = filepath.Dir(h.botPath)
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		h.db.Exec("UPDATE streams SET status=$1 WHERE id=$2", stream.Status, streamID)
		return c.Status(500).JSON(fiber.Map{"error": "failed to start watcher: " + err.Error()})
	}
	go func() {
		err := cmd.Wait()
		if err != nil {
			errStr := err.Error()
			if stderr.Len() > 0 {
				errStr = fmt.Sprintf("%s — %s", err.Error(), stderr.String())
			}
			if len(errStr) > 2000 {
				errStr = errStr[len(errStr)-2000:]
			}
			h.db.Exec("UPDATE streams SET status='error', error_message=$1 WHERE id=$2 AND status NOT IN ('done','error')",
				errStr, streamID)
		}
	}()

	return c.Status(202).JSON(fiber.Map{
		"stream_id": streamID,
		"video_id":  stream.VideoID,
		"status":    "pending",
		"message":   "spikes cleared and watcher restarted",
	})
}
