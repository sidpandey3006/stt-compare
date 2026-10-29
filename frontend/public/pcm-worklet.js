class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super()
    this.samples = new Int16Array(2048)
    this.offset = 0
    this.stopped = false
    this.port.onmessage = event => {
      if (event.data === 'flush') {
        this.flush()
        this.stopped = true
        this.port.postMessage('flushed')
      }
    }
  }

  flush() {
    if (!this.offset) return
    const bytes = new ArrayBuffer(this.offset * 2)
    const view = new DataView(bytes)
    for (let i = 0; i < this.offset; i++) view.setInt16(i * 2, this.samples[i], true)
    this.port.postMessage(bytes, [bytes])
    this.offset = 0
  }

  process(inputs) {
    if (this.stopped) return false
    const input = inputs[0]?.[0]
    if (input) {
      for (let i = 0; i < input.length; i++) {
        const sample = Math.max(-1, Math.min(1, input[i]))
        this.samples[this.offset++] = Math.round(sample * (sample < 0 ? 32768 : 32767))
        if (this.offset === this.samples.length) this.flush()
      }
    }
    return true
  }
}

registerProcessor('pcm-capture', PcmCapture)
