package handlers

import (
	"github.com/gofiber/fiber/v3"
	"github.com/jmoiron/sqlx"

	"chat-watcher-api/internal/models"
)

type ResultsHandler struct {
	db *sqlx.DB
}

func NewResultsHandler(db *sqlx.DB) *ResultsHandler {
	return &ResultsHandler{db: db}
}

// GET /streams/:id/results
func (h *ResultsHandler) GetForStream(c fiber.Ctx) error {
	streamID := c.Params("id")

	var stream models.Stream
	if err := h.db.Get(&stream, `SELECT * FROM streams WHERE id = $1`, streamID); err != nil {
		return fiber.NewError(fiber.StatusNotFound, "stream not found")
	}

	var results []models.SpikeResult
	err := h.db.Select(&results,
		`SELECT * FROM spike_results WHERE stream_id = $1 ORDER BY stream_seconds ASC`,
		streamID,
	)
	if err != nil {
		return fiber.NewError(fiber.StatusInternalServerError, err.Error())
	}

	return c.JSON(fiber.Map{
		"stream": fiber.Map{
			"id":       stream.ID,
			"video_id": stream.VideoID,
			"title":    stream.Title,
			"status":   stream.Status,
		},
		"data":  results,
		"count": len(results),
	})
}

// GET /summary
func (h *ResultsHandler) Summary(c fiber.Ctx) error {
	var summaries []models.StreamSummary
	err := h.db.Select(&summaries, `SELECT * FROM stream_summary ORDER BY id DESC`)
	if err != nil {
		return fiber.NewError(fiber.StatusInternalServerError, err.Error())
	}
	return c.JSON(fiber.Map{"data": summaries, "count": len(summaries)})
}

// GET /summary/:id
func (h *ResultsHandler) SummaryForStream(c fiber.Ctx) error {
	id := c.Params("id")
	var summary models.StreamSummary
	err := h.db.Get(&summary, `SELECT * FROM stream_summary WHERE id = $1`, id)
	if err != nil {
		return fiber.NewError(fiber.StatusNotFound, "stream not found")
	}
	return c.JSON(summary)
}
