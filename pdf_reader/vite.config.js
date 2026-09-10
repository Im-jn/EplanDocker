import { defineConfig } from 'vite'
import { spawn } from 'node:child_process'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(here, '..')

function readBody(req) {
  return new Promise((resolveBody, reject) => {
    let body = ''
    req.setEncoding('utf8')
    req.on('data', (chunk) => {
      body += chunk
    })
    req.on('end', () => resolveBody(body))
    req.on('error', reject)
  })
}

function createVectorApiWorker() {
  const child = spawn('python', ['-u', '-m', 'pdf_parser.vector_api', '--server'], {
    cwd: repoRoot,
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
  })
  const pending = new Map()
  let requestId = 0
  let stderr = ''
  let workerError = null

  const rejectPending = (error) => {
    workerError = error
    for (const request of pending.values()) {
      request.reject(error)
    }
    pending.clear()
  }

  createInterface({ input: child.stdout }).on('line', (line) => {
    try {
      const response = JSON.parse(line)
      const request = pending.get(response.id)
      if (!request) {
        return
      }
      if (response.event === 'progress') {
        request.onProgress?.(response)
        return
      }
      pending.delete(response.id)
      request.resolve(response)
    } catch (error) {
      rejectPending(error)
    }
  })
  child.stderr.on('data', (chunk) => {
    stderr = `${stderr}${chunk}`.slice(-8000)
  })
  child.on('error', (error) => {
    rejectPending(new Error(`Failed to start Python vector worker: ${error.message}`))
  })
  child.on('close', (code) => {
    const error = new Error(stderr || `vector_api.py worker exited with ${code}`)
    rejectPending(error)
  })

  return {
    run(payload, onProgress) {
      return new Promise((resolveResult, reject) => {
        if (workerError || child.exitCode !== null || child.stdin.destroyed || !child.stdin.writable) {
          reject(workerError || new Error('Python vector worker is not running'))
          return
        }
        const id = ++requestId
        pending.set(id, { resolve: resolveResult, reject, onProgress })
        child.stdin.write(`${JSON.stringify({ id, payload })}\n`, (error) => {
          if (error) {
            pending.delete(id)
            reject(error)
          }
        })
      })
    },
    close() {
      child.kill()
    },
  }
}

function vectorMatcherPlugin() {
  let worker
  return {
    name: 'vector-matcher-api',
    configureServer(server) {
      worker = createVectorApiWorker()
      const restartWorker = () => {
        worker?.close()
        worker = createVectorApiWorker()
      }
      server.httpServer?.once('close', () => worker?.close())

      // The Python worker is a long-lived child process, so edits to the
      // pdf_parser sources are not picked up until it is respawned. Watch the
      // package and restart the worker on any .py change.
      const pythonDir = resolve(repoRoot, 'pdf_parser')
      server.watcher.add(pythonDir)
      server.watcher.on('change', (file) => {
        if (file.startsWith(pythonDir) && file.endsWith('.py')) {
          restartWorker()
          server.config.logger.info(`[vector-matcher] restarted Python worker after ${file}`)
        }
      })

      server.middlewares.use('/api/vector-matcher', async (req, res) => {
        if (req.method !== 'POST') {
          res.statusCode = 405
          res.setHeader('Content-Type', 'application/json')
          res.end(JSON.stringify({ ok: false, error: 'Method not allowed' }))
          return
        }
        try {
          const body = await readBody(req)
          const payload = JSON.parse(body || '{}')
          if (payload.mode === 'cancel_extract_info') {
            restartWorker()
            res.statusCode = 200
            res.setHeader('Content-Type', 'application/json')
            res.end(JSON.stringify({ ok: true, symbols_invalidated: true }))
            return
          }
          if (typeof payload.pdf_url === 'string') {
            payload.pdf_path = resolve(here, 'public', payload.pdf_url.replace(/^\//, ''))
          }
          if (payload.mode === 'extract_info') {
            res.statusCode = 200
            res.setHeader('Content-Type', 'application/x-ndjson; charset=utf-8')
            res.setHeader('Cache-Control', 'no-cache, no-transform')
            const result = await worker.run(payload, (progress) => {
              if (!res.destroyed && !res.writableEnded) {
                res.write(`${JSON.stringify(progress)}\n`)
              }
            })
            if (!res.destroyed && !res.writableEnded) {
              res.end(`${JSON.stringify(result)}\n`)
            }
            return
          }
          const result = await worker.run(payload)
          res.statusCode = result.ok ? 200 : 500
          res.setHeader('Content-Type', 'application/json')
          res.end(JSON.stringify(result))
        } catch (error) {
          if (res.destroyed || res.writableEnded) {
            return
          }
          const errorBody = JSON.stringify({
            ok: false,
            error: error instanceof Error ? error.message : String(error),
          })
          if (res.headersSent) {
            res.end(`${errorBody}\n`)
            return
          }
          res.statusCode = 500
          res.setHeader('Content-Type', 'application/json')
          res.end(errorBody)
        }
      })
    },
    closeBundle() {
      worker?.close()
    },
  }
}

export default defineConfig({
  plugins: [vectorMatcherPlugin()],
})
