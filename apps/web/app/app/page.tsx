"use client"
import SoftAurora from "../components/SoftAurora"
export default function Home() {
  return (
 <div style={{ width: '100%', height: '600px', position: 'relative' }}>
  <SoftAurora
    speed={0.6}
    scale={1.5}
    brightness={1}
    color1="#f7f7f7"
    color2="#e100ff"
    bandHeight={0.5}
    bandSpread={1}
    enableMouseInteraction
    mouseInfluence={0}
    spectrum={1}
    colorSpeed={1}
    noiseFrequency={2.5}
    noiseAmplitude={1}
    tilt={0}
    octaveDecay={0.1}
    layerOffset={0}
    clickPulse
    intro
    lightMode={false}
/>
</div>

  );
}
