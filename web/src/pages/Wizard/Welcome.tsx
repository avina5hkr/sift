import { Button, StepHeading, type StepProps } from './ui'

export function Welcome({ next, skip, skipping }: StepProps & { skip: () => void; skipping: boolean }) {
  return (
    <section aria-labelledby="step-title" className="flex flex-col gap-4">
      <StepHeading>Welcome to Sift</StepHeading>
      <p>
        Sift backs up your Android phone&apos;s photos, videos, screenshots, WhatsApp media and documents to your
        Mac, so you can free up space on the phone without worrying.
      </p>
      <p>
        Everything stays on this Mac. Sift has no account, sends nothing anywhere, and only talks to your phone over
        the USB cable. The one exception is a download of Google&apos;s phone tools, and only if you approve it.
      </p>
      <p>Setup takes a few minutes. You will need your phone and its USB cable.</p>
      <div className="flex flex-wrap items-center gap-3">
        <Button primary onClick={next}>
          Get started
        </Button>
        <Button onClick={skip} disabled={skipping}>
          I have already set Sift up
        </Button>
      </div>
    </section>
  )
}
