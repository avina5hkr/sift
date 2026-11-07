import { Button, Hint, StepHeading, type StepProps } from './ui'

// Samsung phones report model names that start with "SM-".
const looksSamsung = (model: string | null | undefined) => /^SM-/i.test(model ?? '')

export function UsbDebugging({ next, model }: StepProps & { model: string | null | undefined }) {
  const samsung = looksSamsung(model)
  return (
    <section aria-labelledby="step-title" className="flex flex-col gap-4">
      <StepHeading>Turn on USB debugging</StepHeading>
      <p>Sift talks to your phone through Android&apos;s USB debugging. It is safe, and you can turn it off again later.</p>
      <ol className="list-decimal space-y-1 pl-6">
        <li>Open Settings, then About phone.</li>
        <li>Open Software information (on some phones this is not needed).</li>
        <li>Tap Build number 7 times. The phone says you are now a developer.</li>
        <li>Go back to Settings and open Developer options.</li>
        <li>Turn on USB debugging.</li>
        <li>Plug the phone into this Mac with a USB cable and unlock it.</li>
        <li>
          If the phone asks &quot;Allow USB debugging?&quot;, tick Always allow from this computer and tap Allow.
        </li>
      </ol>
      <Hint>
        When the phone asks how to use the USB connection, choose File transfer (sometimes called MTP). Charging only
        will not work.
      </Hint>
      <details open={samsung} className="rounded-md border border-neutral-200 p-3 dark:border-neutral-800">
        <summary className="cursor-pointer text-sm font-medium">
          {samsung ? 'Notes for your Samsung phone' : 'Do you have a Samsung phone?'}
        </summary>
        <p className="mt-2 text-sm">
          Samsung phones have a feature called Auto Blocker that stops USB debugging from working. If the phone is not
          found in the next steps, open Settings, then Security and privacy, then Auto Blocker, and turn it off. You can
          turn it on again after the backup.
        </p>
      </details>
      <div>
        <Button primary onClick={next}>
          Continue
        </Button>
      </div>
    </section>
  )
}
