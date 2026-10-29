export const SAMPLE_RATE = 16000
export const MAX_SECONDS = 300
export const MAX_BYTES = 24 * 1024 * 1024

export function wav(chunks: ArrayBuffer[]): Blob {
  const size = chunks.reduce((total, chunk) => total + chunk.byteLength, 0)
  const header = new ArrayBuffer(44)
  const view = new DataView(header)
  const text = (offset: number, value: string) => {
    for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i))
  }
  text(0, 'RIFF')
  view.setUint32(4, size + 36, true)
  text(8, 'WAVE')
  text(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, SAMPLE_RATE, true)
  view.setUint32(28, SAMPLE_RATE * 2, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  text(36, 'data')
  view.setUint32(40, size, true)
  return new Blob([header, ...chunks], { type: 'audio/wav' })
}

export async function capture(onChunk: (data: ArrayBuffer) => void, onEnded: () => void) {
  if (!navigator.mediaDevices?.getUserMedia) throw new Error('Microphone access requires HTTPS or localhost and a supported browser.')
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false } })
  const context = new AudioContext({ sampleRate: SAMPLE_RATE })
  let node: AudioWorkletNode | undefined
  try {
    if (context.sampleRate !== SAMPLE_RATE) throw new Error('This browser cannot capture at 16 kHz. Upload a recording instead.')
    await context.audioWorklet.addModule(`${import.meta.env.BASE_URL}pcm-worklet.js`)
    node = new AudioWorkletNode(context, 'pcm-capture')
    const source = context.createMediaStreamSource(stream)
    const silent = context.createGain()
    silent.gain.value = 0
    node.connect(silent).connect(context.destination)
    node.port.onmessage = (event: MessageEvent<ArrayBuffer>) => onChunk(event.data)
    stream.getAudioTracks()[0].addEventListener('ended', onEnded)
    await context.resume()
    return {
      start() {
        if (stream.getAudioTracks()[0].readyState !== 'live') throw new Error('Microphone disconnected before recording started.')
        source.connect(node!)
      },
      async stop() {
        await new Promise<void>((resolve) => {
          node!.port.onmessage = (event: MessageEvent<ArrayBuffer | string>) => {
            if (event.data === 'flushed') resolve()
            else if (event.data instanceof ArrayBuffer) onChunk(event.data)
          }
          node!.port.postMessage('flush')
          // A suspended worklet cannot acknowledge; the stop still releases the mic.
          setTimeout(resolve, 500)
        })
        node!.disconnect()
        source.disconnect()
        stream.getTracks().forEach(track => track.stop())
        await context.close()
      },
    }
  } catch (error) {
    node?.disconnect()
    stream.getTracks().forEach(track => track.stop())
    await context.close()
    throw error
  }
}
