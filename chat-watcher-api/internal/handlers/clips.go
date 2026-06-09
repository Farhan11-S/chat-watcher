package handlers

import (
	"os/exec"
	"path/filepath"
	"strconv"

	"github.com/gofiber/fiber/v3"
	"github.com/jmoiron/sqlx"
)

type ClipHandler struct {
	db          *sqlx.DB
	clipperPath string
	pythonPath  string
}

func NewClipHandler(db *sqlx.DB, clipperPath, pythonPath string) *ClipHandler {
	return &ClipHandler{db: db, clipperPath: clipperPath, pythonPath: pythonPath}
}

// POST /api/v1/spikes/:spike_id/clip — clip a single spike
func (h *ClipHandler) CreateSpikeJob(c fiber.Ctx) error {
	spikeID, err := strconv.Atoi(c.Params("spike_id"))
	if err != nil {
		return c.Status(400).JSON(fiber.Map{"error": "invalid spike id"})
	}

	// Load spike + stream info
	type SpikeRow struct {
		ID               int     `db:"id"`
		StreamID         int     `db:"stream_id"`
		StreamTimestamp  string  `db:"stream_timestamp"`
		StreamSeconds    float64 `db:"stream_seconds"`
	}
	var spike SpikeRow
	err = h.db.Get(&spike,
		"SELECT id, stream_id, stream_timestamp, stream_seconds FROM spike_results WHERE id = $1",
		spikeID)
	if err != nil {
		return c.Status(404).JSON(fiber.Map{"error": "spike not found"})
	}

	// Check no active job already running for this spike
	var activeCount int
	h.db.Get(&activeCount,
		"SELECT COUNT(*) FROM clip_jobs WHERE spike_id = $1 AND status IN ('pending','processing')",
		spikeID)
	if activeCount > 0 {
		return c.Status(409).JSON(fiber.Map{"error": "a clip job is already running for this spike"})
	}

	// Create job record
	var jobID int
	err = h.db.QueryRow(
		`INSERT INTO clip_jobs (stream_id, spike_id, status, created_at)
		 VALUES ($1, $2, 'pending', NOW()) RETURNING id`,
		spike.StreamID, spikeID,
	).Scan(&jobID)
	if err != nil {
		return c.Status(500).JSON(fiber.Map{"error": "failed to create job: " + err.Error()})
	}

	// Spawn clipper as background process
	cmd := exec.Command(h.pythonPath, h.clipperPath, "clip-spike",
		strconv.Itoa(spike.StreamID),
		strconv.Itoa(spikeID),
		strconv.Itoa(jobID))
	cmd.Dir = filepath.Dir(h.clipperPath)
	if err := cmd.Start(); err != nil {
		h.db.Exec("UPDATE clip_jobs SET status='error', error_msg=$1 WHERE id=$2",
			"failed to start clipper: "+err.Error(), jobID)
		return c.Status(500).JSON(fiber.Map{"error": "failed to start clipper: " + err.Error()})
	}
	go cmd.Wait()

	return c.Status(202).JSON(fiber.Map{
		"job_id":           jobID,
		"stream_id":        spike.StreamID,
		"spike_id":         spikeID,
		"stream_timestamp": spike.StreamTimestamp,
		"status":           "pending",
		"message":          "clip job started",
	})
}

// GET /api/v1/streams/:id/clips — list all clip jobs for a stream
func (h *ClipHandler) ListJobs(c fiber.Ctx) error {
	streamID, err := strconv.Atoi(c.Params("id"))
	if err != nil {
		return c.Status(400).JSON(fiber.Map{"error": "invalid stream id"})
	}

	type ClipJob struct {
		ID             int     `db:"id"               json:"id"`
		StreamID       int     `db:"stream_id"        json:"stream_id"`
		SpikeID        *int    `db:"spike_id"         json:"spike_id"`
		Status         string  `db:"status"           json:"status"`
		ClipsCount     int     `db:"clips_count"      json:"clips_count"`
		DriveFolderURL *string `db:"drive_folder_url" json:"drive_folder_url"`
		ErrorMsg       *string `db:"error_msg"        json:"error_msg"`
		ProgressMsg    *string `db:"progress_msg"     json:"progress_msg"`
		CreatedAt      string  `db:"created_at"       json:"created_at"`
		UpdatedAt      string  `db:"updated_at"       json:"updated_at"`
		FinishedAt     *string `db:"finished_at"      json:"finished_at"`
	}

	var jobs []ClipJob
	err = h.db.Select(&jobs,
		"SELECT * FROM clip_jobs WHERE stream_id = $1 ORDER BY created_at DESC",
		streamID)
	if err != nil {
		return c.Status(500).JSON(fiber.Map{"error": err.Error()})
	}
	if jobs == nil {
		jobs = []ClipJob{}
	}
	return c.JSON(fiber.Map{"stream_id": streamID, "data": jobs})
}

// GET /api/v1/clips/:job_id — poll single job status
func (h *ClipHandler) GetJob(c fiber.Ctx) error {
	jobID, err := strconv.Atoi(c.Params("job_id"))
	if err != nil {
		return c.Status(400).JSON(fiber.Map{"error": "invalid job id"})
	}

	type ClipJob struct {
		ID             int     `db:"id"               json:"id"`
		StreamID       int     `db:"stream_id"        json:"stream_id"`
		SpikeID        *int    `db:"spike_id"         json:"spike_id"`
		Status         string  `db:"status"           json:"status"`
		ClipsCount     int     `db:"clips_count"      json:"clips_count"`
		DriveFolderURL *string `db:"drive_folder_url" json:"drive_folder_url"`
		ErrorMsg       *string `db:"error_msg"        json:"error_msg"`
		ProgressMsg    *string `db:"progress_msg"     json:"progress_msg"`
		CreatedAt      string  `db:"created_at"       json:"created_at"`
		UpdatedAt      string  `db:"updated_at"       json:"updated_at"`
		FinishedAt     *string `db:"finished_at"      json:"finished_at"`
	}

	var job ClipJob
	if err := h.db.Get(&job, "SELECT * FROM clip_jobs WHERE id = $1", jobID); err != nil {
		return c.Status(404).JSON(fiber.Map{"error": "job not found"})
	}
	return c.JSON(job)
}
