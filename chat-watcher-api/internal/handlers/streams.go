package handlers

import (
	"github.com/gofiber/fiber/v3"
	"github.com/jmoiron/sqlx"

	"chat-watcher-api/internal/models"
)

type StreamHandler struct {
	db *sqlx.DB
}

func NewStreamHandler(db *sqlx.DB) *StreamHandler {
	return &StreamHandler{db: db}
}

// GET /streams
func (h *StreamHandler) List(c fiber.Ctx) error {
	var streams []models.Stream
	err := h.db.Select(&streams, `SELECT * FROM streams ORDER BY id DESC`)
	if err != nil {
		return fiber.NewError(fiber.StatusInternalServerError, err.Error())
	}
	return c.JSON(fiber.Map{"data": streams, "count": len(streams)})
}

// GET /streams/:id
func (h *StreamHandler) Get(c fiber.Ctx) error {
	id := c.Params("id")
	var stream models.Stream
	err := h.db.Get(&stream, `SELECT * FROM streams WHERE id = $1`, id)
	if err != nil {
		return fiber.NewError(fiber.StatusNotFound, "stream not found")
	}
	return c.JSON(stream)
}

// POST /streams
func (h *StreamHandler) Create(c fiber.Ctx) error {
	var req models.CreateStreamRequest
	if err := c.Bind().Body(&req); err != nil {
		return fiber.NewError(fiber.StatusBadRequest, "invalid request body")
	}
	if req.URL == "" {
		return fiber.NewError(fiber.StatusBadRequest, "url is required")
	}

	// Extract video_id from URL
	videoID, err := extractVideoID(req.URL)
	if err != nil {
		return fiber.NewError(fiber.StatusBadRequest, err.Error())
	}

	var stream models.Stream
	err = h.db.QueryRowx(
		`INSERT INTO streams (video_id, url)
		 VALUES ($1, $2)
		 ON CONFLICT (video_id) DO UPDATE SET video_id = EXCLUDED.video_id
		 RETURNING *`,
		videoID, req.URL,
	).StructScan(&stream)
	if err != nil {
		return fiber.NewError(fiber.StatusInternalServerError, err.Error())
	}

	return c.Status(fiber.StatusCreated).JSON(stream)
}

// PATCH /streams/:id
func (h *StreamHandler) Update(c fiber.Ctx) error {
	id := c.Params("id")

	// Ensure stream exists
	var existing models.Stream
	if err := h.db.Get(&existing, `SELECT * FROM streams WHERE id = $1`, id); err != nil {
		return fiber.NewError(fiber.StatusNotFound, "stream not found")
	}

	var req models.UpdateStreamRequest
	if err := c.Bind().Body(&req); err != nil {
		return fiber.NewError(fiber.StatusBadRequest, "invalid request body")
	}

	// Apply partial updates
	if req.URL != nil {
		existing.URL = *req.URL
	}
	if req.Status != nil {
		validStatuses := map[string]bool{"pending": true, "processing": true, "done": true, "error": true}
		if !validStatuses[*req.Status] {
			return fiber.NewError(fiber.StatusBadRequest, "invalid status: must be pending, processing, done, or error")
		}
		existing.Status = *req.Status
	}

	var updated models.Stream
	err := h.db.QueryRowx(
		`UPDATE streams SET url = $1, status = $2 WHERE id = $3 RETURNING *`,
		existing.URL, existing.Status, id,
	).StructScan(&updated)
	if err != nil {
		return fiber.NewError(fiber.StatusInternalServerError, err.Error())
	}

	return c.JSON(updated)
}

// DELETE /streams/:id
func (h *StreamHandler) Delete(c fiber.Ctx) error {
	id := c.Params("id")

	result, err := h.db.Exec(`DELETE FROM streams WHERE id = $1`, id)
	if err != nil {
		return fiber.NewError(fiber.StatusInternalServerError, err.Error())
	}

	rows, _ := result.RowsAffected()
	if rows == 0 {
		return fiber.NewError(fiber.StatusNotFound, "stream not found")
	}

	return c.SendStatus(fiber.StatusNoContent)
}
