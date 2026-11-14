// Plain-English labels and help for every setting the Settings pages show (keys from settings.py PAGES).
export type FieldText = { label: string; help: string }

export const LABELS: Record<string, FieldText> = {
  'cleanup.precheck_suggestions': {
    label: 'Tick junk suggestions for me (beta)',
    help: 'On: after a calibration passes, suggested junk starts ticked. Off: you tick every suggestion yourself. Exact duplicate copies are ticked either way.',
  },
  'phone.keep_awake': {
    label: 'Keep the phone awake during jobs',
    help: 'While Sift works, it keeps the phone screen on and the Mac from sleeping.',
  },
  'phone.allow_delete_over_wifi': {
    label: 'Allow delete over Wi-Fi',
    help: 'Off: deleting from the phone needs a USB cable.',
  },
  'phone.parallel_reads_usb': {
    label: 'Files read at once over USB',
    help: 'More can be faster, up to what the phone and cable allow. The speed test can pick this for you.',
  },
  'phone.parallel_reads_wifi': {
    label: 'Files read at once over Wi-Fi',
    help: 'Wi-Fi usually does best with fewer at once.',
  },
  'phone.heartbeat_seconds': {
    label: 'Phone check interval (seconds)',
    help: 'How often Sift checks that the phone is still there during a job.',
  },
  'phone.adb_path': {
    label: 'adb program path',
    help: 'Leave empty to use the adb Sift manages itself.',
  },
  'phone.adb_server_port': {
    label: 'adb server port',
    help: 'The port Sift’s own adb server listens on. Change it only if another program uses 5037.',
  },
  'backup.required_destinations': {
    label: 'Required destinations',
    help: 'A file counts as backed up, and may be deleted from the phone, only once it is in every ticked place.',
  },
  'backup.layout': {
    label: 'Folder layout',
    help: 'Mirror keeps the phone’s folders. By date sorts new files into year and month folders.',
  },
  'backup.free_space_headroom_pct': {
    label: 'Free-space headroom (%)',
    help: 'A backup will not start unless this much of the drive would stay free afterwards.',
  },
  'backup.include_whatsapp_db': {
    label: 'Include WhatsApp databases',
    help: 'Back up the chat database files too. They are never deleted from the phone.',
  },
  'backup.include_whatsapp_statuses': {
    label: 'Include WhatsApp Statuses',
    help: 'Back up the Statuses folder. It is a cache that WhatsApp clears by itself.',
  },
  'backup.skip_hidden_folders': {
    label: 'Hidden folders to skip',
    help: 'One folder name per line. Files inside are not backed up.',
  },
  'backup.skip_name_prefixes': {
    label: 'File name starts to skip',
    help: 'One per line. Files whose name starts with this are not backed up.',
  },
  'backup.skip_name_suffixes': {
    label: 'File name ends to skip',
    help: 'One per line. Files whose name ends with this are not backed up.',
  },
  'backup.skip_zero_byte': {
    label: 'Skip empty files',
    help: 'Files with no content are not backed up.',
  },
  'backup.settle_seconds': {
    label: 'Settle time (seconds)',
    help: 'A file changed this recently is left for the next backup, so half-written files are not copied.',
  },
  'backup.max_retries': {
    label: 'Retries per file',
    help: 'How many times Sift tries a file again before it counts as failed.',
  },
  'backup.confirmation_valid_days': {
    label: 'Confirmation valid for (days)',
    help: 'How long your “the backup is good” confirmation counts before Sift asks again.',
  },
  'backup.verify_reminder_days': {
    label: 'Verify reminder (days)',
    help: 'After this many days, Sift reminds you to check the backup again.',
  },
  'cleanup.delete_roots': {
    label: 'Folders Cleanup may delete from',
    help: 'One phone folder per line. Nothing outside these is ever offered for deletion.',
  },
  'cleanup.never_delete': {
    label: 'Never-delete list',
    help: 'One phone folder per line. The built-in ones stay on the list even if you remove them.',
  },
  'cleanup.min_age_days': {
    label: 'Minimum age (days)',
    help: 'Files newer than this are never offered for deletion.',
  },
  'cleanup.quarantine_days': {
    label: 'Quarantine days',
    help: 'Deleted files wait in Quarantine this long before they are gone for good.',
  },
  'scoring.protect_screenshots': {
    label: 'Protect screenshots',
    help: 'Screenshots are never suggested as junk.',
  },
  'scoring.protected_folders': {
    label: 'Protected folders',
    help: 'One phone folder per line. Pictures in them are never suggested as junk.',
  },
  'browse.never_delete_extra': {
    label: 'Extra never-delete folders',
    help: 'One phone folder per line. Browse will not offer to delete anything inside.',
  },
  'browse.large_video_mb': {
    label: 'Large video size (MB)',
    help: 'Videos at least this big are listed as large.',
  },
  'browse.old_days': {
    label: 'Old file age (days)',
    help: 'Files untouched for this long are listed as old.',
  },
  'slate.keep_folders': {
    label: 'Clean slate: folders to keep',
    help: 'One phone folder per line. The clean-slate plan leaves these alone.',
  },
  'slate.keep_newer_than_days': {
    label: 'Clean slate: keep files newer than (days)',
    help: 'Recent files are kept on the phone by the clean-slate plan.',
  },
  'apps.unused_days': {
    label: 'Unused app after (days)',
    help: 'Apps not opened for this long are listed as unused.',
  },
  'apps.protected_packages': {
    label: 'Protected apps',
    help: 'One app package name per line. Sift never offers to remove these.',
  },
  'analysis.ocr_languages': {
    label: 'Text reading languages',
    help: 'One language code per line, for example en-US.',
  },
  'analysis.workers': {
    label: 'Analysis workers',
    help: '0 lets Sift choose from the number of processor cores.',
  },
  'scoring.t_suggest': {
    label: 'Suggest threshold (pictures)',
    help: 'How sure Sift must be before it suggests a picture as junk. Between 0 and 1; higher means fewer suggestions.',
  },
  'scoring.t_suggest_video': {
    label: 'Suggest threshold (videos)',
    help: 'The same, for videos.',
  },
  'scoring.t_maybe': {
    label: 'Maybe threshold',
    help: 'Below the suggest threshold but above this, a file is listed as “maybe”.',
  },
  'scoring.min_labels_each': {
    label: 'Labels needed before suggesting',
    help: 'How many keep and junk examples you must give before suggestions start.',
  },
  'analysis.cluster_cosine': {
    label: 'Group similarity',
    help: 'How alike pictures must look to be grouped together. Between 0 and 1.',
  },
  'cleanup.max_delete_per_run': {
    label: 'Most files per delete run',
    help: 'A single delete run stops at this many files.',
  },
  'health.storage_warn_pct': {
    label: 'Storage warning (% full)',
    help: 'The health report warns when the phone is this full.',
  },
  'health.battery_temp_warn_c': {
    label: 'Battery temperature warning (°C)',
    help: 'The health report warns above this temperature.',
  },
}

export const PAGE_TITLES: [string, string][] = [
  ['general', 'General'],
  ['phone', 'Phone'],
  ['destinations', 'Destinations'],
  ['backup', 'Backup'],
  ['cleanup', 'Cleanup'],
  ['models', 'Models'],
  ['advanced', 'Advanced'],
]

export const ENUM_TEXT: Record<string, string> = {
  mirror: 'Mirror the phone’s folders',
  by_date: 'By date',
  auto: 'Auto',
  vision: 'Apple Vision',
  tesseract: 'Tesseract',
  mac: 'The Mac folder',
  ssd: 'The SSD',
}
