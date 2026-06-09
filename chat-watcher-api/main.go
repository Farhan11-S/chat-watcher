package main

import (
	"log"

	"github.com/gofiber/fiber/v3"
	"github.com/gofiber/fiber/v3/middleware/cors"
	"github.com/gofiber/fiber/v3/middleware/logger"
	"github.com/gofiber/fiber/v3/middleware/recover"
	"github.com/gofiber/fiber/v3/middleware/static"

	"chat-watcher-api/config"
	"chat-watcher-api/internal/db"
	"chat-watcher-api/internal/handlers"
)

func main() {
	// Load config
	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("failed to load config: %v", err)
	}

	// Connect to DB
	database, err := db.Connect(cfg.DSN())
	if err != nil {
		log.Fatalf("failed to connect to database: %v", err)
	}
	defer database.Close()
	log.Println("✅ Connected to PostgreSQL")

	// Init Fiber v3
	app := fiber.New(fiber.Config{
		AppName:      "Chat Watcher API v1.0",
		ErrorHandler: errorHandler,
	})

	// Middleware
	app.Use(recover.New())
	app.Use(logger.New(logger.Config{
		Format: "[${time}] ${status} ${method} ${path} ${latency}\n",
	}))
	app.Use(cors.New())

	// Handlers
	streamHandler  := handlers.NewStreamHandler(database)
	configHandler  := handlers.NewConfigHandler(database)
	resultsHandler := handlers.NewResultsHandler(database)
	clipHandler    := handlers.NewClipHandler(database, cfg.ClipperPath, cfg.PythonPath)
	watcherHandler := handlers.NewWatcherHandler(database, cfg.BotPath, cfg.PythonPath)
	summarizeHandler := handlers.NewSummarizeHandler(database, cfg.SummarizerEndpoint, cfg.SummarizerAPIKey, cfg.SummarizerModel)

	// ── Routes ────────────────────────────────────────────────────────────────

	// Health check
	app.Get("/health", func(c fiber.Ctx) error {
		return c.JSON(fiber.Map{"status": "ok", "app": "chat-watcher-api"})
	})

	api := app.Group("/api/v1")

	// Streams CRUD
	streams := api.Group("/streams")
	streams.Get("/", streamHandler.List)
	streams.Post("/", streamHandler.Create)
	streams.Get("/:id", streamHandler.Get)
	streams.Patch("/:id", streamHandler.Update)
	streams.Delete("/:id", streamHandler.Delete)

	// Per-stream spike config
	streams.Get("/:id/config", configHandler.GetForStream)
	streams.Put("/:id/config", configHandler.UpsertForStream)
	streams.Delete("/:id/config", configHandler.DeleteForStream)

	// Per-stream spike results
	streams.Get("/:id/results", resultsHandler.GetForStream)

	// Run watcher for a stream
	streams.Post("/:id/run", watcherHandler.RunStream)
	streams.Post("/:id/rerun", watcherHandler.RerunStream)

	// Clip jobs — per spike
	api.Post("/spikes/:spike_id/clip", clipHandler.CreateSpikeJob)
	streams.Get("/:id/clips", clipHandler.ListJobs)
	api.Get("/clips/:job_id", clipHandler.GetJob)

	// Summarize — on-demand LLM context summary per spike
	api.Post("/spikes/:spike_id/summarize", summarizeHandler.SummarizeSpike)
	api.Get("/spikes/:spike_id", summarizeHandler.GetSpike)
	streams.Post("/:id/summarize-all", summarizeHandler.SummarizeAll)

	// Global spike config
	api.Get("/config", configHandler.GetGlobal)
	api.Put("/config", configHandler.UpsertGlobal)

	// Summary views
	api.Get("/summary", resultsHandler.Summary)
	api.Get("/summary/:id", resultsHandler.SummaryForStream)

	// Static files (frontend) — v3 middleware pattern
	// Must be registered last so API routes take priority
	app.Use("/", static.New("./static", static.Config{
		IndexNames: []string{"index.html"},
		Browse:     false,
		Compress:   true,
	}))

	// Start server
	log.Printf("🚀 Server starting on port %s", cfg.Port)
	log.Fatal(app.Listen(":" + cfg.Port))
}

// errorHandler returns consistent JSON error responses
func errorHandler(c fiber.Ctx, err error) error {
	code := fiber.StatusInternalServerError
	msg := "internal server error"

	if e, ok := err.(*fiber.Error); ok {
		code = e.Code
		msg = e.Message
	}

	return c.Status(code).JSON(fiber.Map{
		"error":  msg,
		"status": code,
	})
}
