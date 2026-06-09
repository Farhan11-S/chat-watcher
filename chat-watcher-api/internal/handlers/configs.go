package handlers

import (
	"github.com/gofiber/fiber/v3"
	"github.com/jmoiron/sqlx"

	"chat-watcher-api/internal/models"
)

type ConfigHandler struct {
	db *sqlx.DB
}

func NewConfigHandler(db *sqlx.DB) *ConfigHandler {
	return &ConfigHandler{db: db}
}

// GET /config
func (h *ConfigHandler) GetGlobal(c fiber.Ctx) error {
	var cfg models.SpikeConfig
	err := h.db.Get(&cfg, `SELECT * FROM spike_config WHERE stream_id IS NULL`)
	if err != nil {
		return fiber.NewError(fiber.StatusNotFound, "global config not found")
	}
	return c.JSON(cfg)
}

// PUT /config
func (h *ConfigHandler) UpsertGlobal(c fiber.Ctx) error {
	var req models.UpsertSpikeConfigRequest
	if err := c.Bind().Body(&req); err != nil {
		return fiber.NewError(fiber.StatusBadRequest, "invalid request body")
	}
	if err := validateConfigRequest(&req); err != nil {
		return fiber.NewError(fiber.StatusBadRequest, err.Error())
	}

	var cfg models.SpikeConfig
	err := h.db.QueryRowx(
		`INSERT INTO spike_config (stream_id, window_seconds, baseline_windows, spike_multiplier, min_messages, cooldown_seconds, clip_window_secs, clip_resolution)
		 VALUES (NULL, $1, $2, $3, $4, $5, $6, $7)
		 ON CONFLICT (stream_id) DO UPDATE SET
		   window_seconds   = EXCLUDED.window_seconds,
		   baseline_windows = EXCLUDED.baseline_windows,
		   spike_multiplier = EXCLUDED.spike_multiplier,
		   min_messages     = EXCLUDED.min_messages,
		   cooldown_seconds = EXCLUDED.cooldown_seconds,
		   clip_window_secs = EXCLUDED.clip_window_secs,
		   clip_resolution  = EXCLUDED.clip_resolution
		 RETURNING *`,
		req.WindowSeconds, req.BaselineWindows, req.SpikeMultiplier,
		req.MinMessages, req.CooldownSeconds, req.ClipWindowSecs, req.ClipResolution,
	).StructScan(&cfg)
	if err != nil {
		return fiber.NewError(fiber.StatusInternalServerError, err.Error())
	}

	return c.JSON(cfg)
}

// GET /streams/:id/config
func (h *ConfigHandler) GetForStream(c fiber.Ctx) error {
	streamID := c.Params("id")

	var count int
	if err := h.db.Get(&count, `SELECT COUNT(*) FROM streams WHERE id = $1`, streamID); err != nil || count == 0 {
		return fiber.NewError(fiber.StatusNotFound, "stream not found")
	}

	var cfg models.SpikeConfig
	err := h.db.Get(&cfg, `SELECT * FROM spike_config WHERE stream_id = $1`, streamID)
	if err != nil {
		// Fall back to global default
		if err := h.db.Get(&cfg, `SELECT * FROM spike_config WHERE stream_id IS NULL`); err != nil {
			return fiber.NewError(fiber.StatusNotFound, "no config found")
		}
		return c.JSON(fiber.Map{"data": cfg, "source": "global_default"})
	}

	return c.JSON(fiber.Map{"data": cfg, "source": "per_stream"})
}

// PUT /streams/:id/config
func (h *ConfigHandler) UpsertForStream(c fiber.Ctx) error {
	streamID := c.Params("id")

	var count int
	if err := h.db.Get(&count, `SELECT COUNT(*) FROM streams WHERE id = $1`, streamID); err != nil || count == 0 {
		return fiber.NewError(fiber.StatusNotFound, "stream not found")
	}

	var req models.UpsertSpikeConfigRequest
	if err := c.Bind().Body(&req); err != nil {
		return fiber.NewError(fiber.StatusBadRequest, "invalid request body")
	}
	if err := validateConfigRequest(&req); err != nil {
		return fiber.NewError(fiber.StatusBadRequest, err.Error())
	}

	var cfg models.SpikeConfig
	err := h.db.QueryRowx(
		`INSERT INTO spike_config (stream_id, window_seconds, baseline_windows, spike_multiplier, min_messages, cooldown_seconds, clip_window_secs, clip_resolution)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
		 ON CONFLICT (stream_id) DO UPDATE SET
		   window_seconds   = EXCLUDED.window_seconds,
		   baseline_windows = EXCLUDED.baseline_windows,
		   spike_multiplier = EXCLUDED.spike_multiplier,
		   min_messages     = EXCLUDED.min_messages,
		   cooldown_seconds = EXCLUDED.cooldown_seconds,
		   clip_window_secs = EXCLUDED.clip_window_secs,
		   clip_resolution  = EXCLUDED.clip_resolution
		 RETURNING *`,
		streamID, req.WindowSeconds, req.BaselineWindows, req.SpikeMultiplier,
		req.MinMessages, req.CooldownSeconds, req.ClipWindowSecs, req.ClipResolution,
	).StructScan(&cfg)
	if err != nil {
		return fiber.NewError(fiber.StatusInternalServerError, err.Error())
	}

	return c.JSON(cfg)
}

// DELETE /streams/:id/config
func (h *ConfigHandler) DeleteForStream(c fiber.Ctx) error {
	streamID := c.Params("id")

	result, err := h.db.Exec(`DELETE FROM spike_config WHERE stream_id = $1`, streamID)
	if err != nil {
		return fiber.NewError(fiber.StatusInternalServerError, err.Error())
	}

	rows, _ := result.RowsAffected()
	if rows == 0 {
		return fiber.NewError(fiber.StatusNotFound, "no per-stream config found")
	}

	return c.SendStatus(fiber.StatusNoContent)
}

func validateConfigRequest(req *models.UpsertSpikeConfigRequest) error {
	if req.WindowSeconds <= 0 {
		return fiber.NewError(fiber.StatusBadRequest, "window_seconds must be > 0")
	}
	if req.BaselineWindows <= 0 {
		return fiber.NewError(fiber.StatusBadRequest, "baseline_windows must be > 0")
	}
	if req.SpikeMultiplier <= 1.0 {
		return fiber.NewError(fiber.StatusBadRequest, "spike_multiplier must be > 1.0")
	}
	if req.MinMessages <= 0 {
		return fiber.NewError(fiber.StatusBadRequest, "min_messages must be > 0")
	}
	if req.CooldownSeconds <= 0 {
		return fiber.NewError(fiber.StatusBadRequest, "cooldown_seconds must be > 0")
	}
	if req.ClipWindowSecs <= 0 {
		return fiber.NewError(fiber.StatusBadRequest, "clip_window_secs must be > 0")
	}
	validResolutions := map[string]bool{"360p": true, "480p": true, "720p": true, "1080p": true, "best": true}
	if req.ClipResolution == "" {
		req.ClipResolution = "best"
	}
	if !validResolutions[req.ClipResolution] {
		return fiber.NewError(fiber.StatusBadRequest, "clip_resolution must be one of: 360p, 480p, 720p, 1080p, best")
	}
	return nil
}
