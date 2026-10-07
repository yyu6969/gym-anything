# Source data

`dotfiles.bundle` is a full Git bundle of the public
[`ericwbailey/dotfiles`](https://github.com/ericwbailey/dotfiles) repository.
The environment pins branch `main` to upstream commit
`218b5e72424aca8b580e52342dbb92bd4bd076c8`, authored by Eric Bailey with the
message `Update .macos`. This is the exact commit and repository content visible
in the supplied demonstration video. It contains no license file at task start.

`a11y-webring.bundle` is a full Git bundle of the public
[`ericwbailey/a11y-webring.club`](https://github.com/ericwbailey/a11y-webring.club)
repository. It preserves the two real revisions needed to reconstruct upstream
pull request #40, "Add verification functions":

- `video-main` is `83d965d99adaf5adaee708fb093fa81063cd0db2`, the historical
  target revision at which the pull request branch was 90 commits behind.
- `video-source` is `4817a445d1b74904bd695059aea63705370f9205`, the
  four-commit `davepgreene/add-verification-function` pull request head.

The bundle is self-contained and has SHA-256
`cc1fb6238313ddecbf6ee541a80c36a19d17e26df0d860e64f12963912a45cc6`.

`a11yproject-distractors.bundle` is a full Git bundle of the public
[`a11yproject/a11yproject.com`](https://github.com/a11yproject/a11yproject.com)
history needed for the two additional open assigned merge requests visible in
the demonstration:

- `video-main` is `a303bf71be0a2673f1a81096291e0e963dd80e0d`.
- `pr-1485` is the real `update or remove 404 links` head
  `352da2a0bb2ff347bdb0ffe0007f26afc1aebd2c` by Roshan Jossy.
- `pr-1270` is the real `feat: add WCAG levels` head
  `7b5c8cbff2aff1b474e3b3877671b83bdc6a734b` by Agustina Chaer.

The bundle records a complete history and has SHA-256
`716728adf25ccef111fdacf1124b172063f80726a54551df958414c6a291fe19`.
