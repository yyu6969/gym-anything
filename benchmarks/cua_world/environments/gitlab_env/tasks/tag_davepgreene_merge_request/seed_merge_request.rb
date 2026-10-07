# Seed/reset the real upstream pull request represented by the demonstrated
# GitLab merge request. TASK_SEED_PHASE=prepare creates the identities and
# empty repository before Git pushes; finalize creates and resets merge request
# !40 after the real branches are present.

phase = ENV.fetch('TASK_SEED_PHASE', 'finalize')

task_user = User.find_by!(username: 'byteblaze')

author = User.find_or_initialize_by(username: 'davepgreene')
author.name = 'Dave Greene'
author.email = 'davepgreene@example.test'
author.password = 'R7mV4qL9dP2s!'
author.password_confirmation = 'R7mV4qL9dP2s!'
author.admin = false
author.skip_confirmation! if author.respond_to?(:skip_confirmation!)
author.save!

roshan = User.find_or_initialize_by(username: 'roshanjossey')
roshan.name = 'Roshan Jossy'
roshan.email = 'roshanjossey@example.test'
roshan.password = 'M6vR2pL8sQ4n!'
roshan.password_confirmation = 'M6vR2pL8sQ4n!'
roshan.admin = false
roshan.skip_confirmation! if roshan.respond_to?(:skip_confirmation!)
roshan.save!

agustina = User.find_or_initialize_by(username: 'agch-dev')
agustina.name = 'Agustina Chaer'
agustina.email = 'agch-dev@example.test'
agustina.password = 'N4qT8mK2vR6s!'
agustina.password_confirmation = 'N4qT8mK2vR6s!'
agustina.admin = false
agustina.skip_confirmation! if agustina.respond_to?(:skip_confirmation!)
agustina.save!

project = Project.find_or_initialize_by(
  namespace_id: task_user.namespace.id,
  path: 'a11y-webring-club'
)
project.name = 'a11y-webring.club'
project.description = '🌐 A webring for digital accessibility practitioners.'
project.visibility_level = Gitlab::VisibilityLevel::PUBLIC
project.creator = task_user if project.respond_to?(:creator=)
project.save!
project.add_owner(task_user) unless project.team.member?(task_user)

project.create_repository unless project.repository_exists?
project.add_developer(author) unless project.team.member?(author)

# The demonstration's assigned list includes two genuine A11Y Project pull
# requests as title-selection distractors. Reuse the canonical shared project
# record, but make its repository and MR state task-owned and repeatable.
a11y_group = Group.find_by!(path: 'the-a11y-project')
distractor_project = Project.find_by!(
  namespace_id: a11y_group.id,
  path: 'a11yproject-com'
)
distractor_project.create_repository unless distractor_project.repository_exists?
distractor_project.add_developer(roshan) unless distractor_project.team.member?(roshan)
distractor_project.add_developer(agustina) unless distractor_project.team.member?(agustina)

if phase == 'prepare'
  puts [
    'TASK_MR_PREPARE_OK',
    "project=#{project.full_path}",
    "author=#{author.username}",
    "distractor_project=#{distractor_project.full_path}",
    'distractor_authors=roshanjossey,agch-dev'
  ].join(' ')
  exit
end

unless %w[finalize settle].include?(phase)
  raise "unknown TASK_SEED_PHASE=#{phase}"
end

source_branch = 'github/fork/davepgreene/add-verification-function'
target_branch = 'main'
title = 'Add verification functions'
description = <<~MARKDOWN.chomp
  This PR adds two different implementations of a way to verify members of the webring.

  The first, `verify.mjs` simply makes sure that the listed URL only returns a response in the `2xx` range. This is a quick spot check to make sure that the site owner doesn't redirect to a different location. The second, `verifyDNS.mjs` is slightly more complicated. It verifies, using DNS TXT records, whether a site owner controls their own domain by requiring a TXT record on the listed hostname. Both of these functions use Netlify's scheduled functions feature. `verify` runs daily and `verifyDNS` runs weekly. Obviously that timing can be changed by updating the value in `netlify.toml`.

  Neither of these implementations solves the question as to what to do if one of these functions actually encounters that scenario. I don't know much about Netlify or whether it has logging or alerting functionality. Hopefully this helps in the discussion in #33.
MARKDOWN

raise "missing #{target_branch} branch" unless project.repository.branch_exists?(target_branch)
raise "missing #{source_branch} branch" unless project.repository.branch_exists?(source_branch)

merge_request = project.merge_requests.find_by(iid: 40)
if merge_request && merge_request.title != title
  raise "merge request !40 is unexpectedly titled #{merge_request.title.inspect}"
end

unless merge_request
  stale = project.merge_requests.find_by(title: title, source_branch: source_branch)
  stale.destroy! if stale

  merge_request = MergeRequests::CreateService.new(
    project: project,
    current_user: author,
    params: {
      iid: 40,
      title: title,
      description: description,
      source_branch: source_branch,
      target_branch: target_branch,
      assignee_ids: [task_user.id],
      squash: false
    }
  ).execute

  unless merge_request.persisted?
    raise "failed to create merge request: #{merge_request.errors.full_messages.join(', ')}"
  end
end

merge_request.assign_attributes(
  title: title,
  description: description,
  author: author,
  source_project: project,
  target_project: project,
  source_branch: source_branch,
  target_branch: target_branch,
  state_id: MergeRequest.available_states.fetch(:opened),
  squash: false,
  merge_user_id: nil,
  merge_commit_sha: nil,
  merge_params: {}
)
merge_request.assignees = [task_user]
merge_request.save!

# The demonstrated episode begins with no discussion comments. Remove only
# non-system notes from this task-owned merge request so repeated episodes do
# not inherit a previous agent's mention.
removed_notes = merge_request.notes.where(system: false).delete_all

distractor_specs = [
  {
    iid: 1485,
    title: 'update or remove 404 links',
    author: roshan,
    source_branch: 'github/fork/Roshanjossey/1478-fix-404-urls',
    description: <<~MARKDOWN.chomp,
      https://github.com/a11yproject/a11yproject.com/issues/1478

      Remove broken resource links and update moved accessibility resources,
      browser extensions, meetups, books, and articles to their current URLs.
    MARKDOWN
    created_at: 1.year.ago,
    labels: []
  },
  {
    iid: 1270,
    title: 'feat: add WCAG levels',
    author: agustina,
    source_branch: 'github/fork/agch-dev/feat/add-wcag-levels',
    description: <<~MARKDOWN.chomp,
      Closes #1209

      ## Summary
      This PR adds the WCAG level to each checklist item so it's easier to know
      what checks to prioritize when trying to make a site accessible.
    MARKDOWN
    created_at: 2.years.ago,
    labels: %w[data javascript markup styling]
  }
]

distractor_merge_requests = distractor_specs.map do |spec|
  raise "missing distractor main branch" unless distractor_project.repository.branch_exists?('main')
  unless distractor_project.repository.branch_exists?(spec.fetch(:source_branch))
    raise "missing #{spec.fetch(:source_branch)} branch"
  end

  distractor = distractor_project.merge_requests.find_by(iid: spec.fetch(:iid))
  if distractor && distractor.title != spec.fetch(:title)
    raise "merge request !#{spec.fetch(:iid)} is unexpectedly titled #{distractor.title.inspect}"
  end

  unless distractor
    stale = distractor_project.merge_requests.find_by(
      title: spec.fetch(:title),
      source_branch: spec.fetch(:source_branch)
    )
    stale.destroy! if stale

    distractor = MergeRequests::CreateService.new(
      project: distractor_project,
      current_user: spec.fetch(:author),
      params: {
        iid: spec.fetch(:iid),
        title: spec.fetch(:title),
        description: spec.fetch(:description),
        source_branch: spec.fetch(:source_branch),
        target_branch: 'main',
        assignee_ids: [task_user.id],
        squash: false
      }
    ).execute

    unless distractor.persisted?
      raise "failed to create distractor !#{spec.fetch(:iid)}: #{distractor.errors.full_messages.join(', ')}"
    end
  end

  labels = spec.fetch(:labels).map do |title|
    label = distractor_project.labels.find_or_initialize_by(title: title)
    label.color = '#D9A5B3'
    label.save!
    label
  end

  distractor.assign_attributes(
    title: spec.fetch(:title),
    description: spec.fetch(:description),
    author: spec.fetch(:author),
    source_project: distractor_project,
    target_project: distractor_project,
    source_branch: spec.fetch(:source_branch),
    target_branch: 'main',
    state_id: MergeRequest.available_states.fetch(:opened),
    squash: false,
    merge_user_id: nil,
    merge_commit_sha: nil,
    merge_params: {}
  )
  distractor.assignees = [task_user]
  distractor.labels = labels
  distractor.save!
  distractor.notes.where(system: false).delete_all
  distractor.update_columns(
    created_at: spec.fetch(:created_at),
    updated_at: 9.months.ago
  )
  distractor.notes.where(system: true).update_all(
    created_at: 9.months.ago,
    updated_at: 9.months.ago
  )
  distractor
end

# Keep the relative ages shown in the source UI while preserving the real
# upstream branch contents and pull-request metadata.
historical_created_at = 11.months.ago - 2.days
historical_activity_at = 9.months.ago
merge_request.update_columns(
  created_at: historical_created_at,
  updated_at: historical_activity_at
)
merge_request.notes.where(system: true).update_all(
  created_at: historical_activity_at,
  updated_at: historical_activity_at
)

# Pushing the authentic source branch creates the same Auto DevOps pipeline
# shown in the recording. There is deliberately no runner in this environment,
# so make that pipeline's demonstrated failed terminal state deterministic
# instead of leaving it pending indefinitely.
fail_pipeline = lambda do |pipeline_project, pipeline_branch|
  source_sha = pipeline_project.repository.commit(pipeline_branch)&.sha
  raise "could not resolve #{pipeline_branch} commit" unless source_sha

  source_pipeline = nil
  30.times do
    source_pipeline = pipeline_project.ci_pipelines
      .where(sha: source_sha)
      .order(id: :desc)
      .first
    break if source_pipeline

    sleep 2
  end
  raise "pipeline was not created for #{pipeline_branch}" unless source_pipeline

  source_pipeline.builds.update_all(
    status: 'failed',
    finished_at: historical_activity_at,
    updated_at: historical_activity_at
  )
  source_pipeline.update_columns(
    status: 'failed',
    finished_at: historical_activity_at,
    updated_at: historical_activity_at
  )
  source_pipeline
end

pipeline = fail_pipeline.call(project, source_branch)
distractor_pipelines = distractor_merge_requests.map do |distractor|
  fail_pipeline.call(distractor_project, distractor.source_branch)
end

# Reapply historical activity ages after any asynchronous new-MR processing.
merge_request.update_columns(updated_at: historical_activity_at)
distractor_merge_requests.each do |distractor|
  distractor.update_columns(updated_at: historical_activity_at)
end
merge_request.reload

raise 'merge request iid drifted' unless merge_request.iid == 40
raise 'merge request is not open' unless merge_request.open?
raise 'wrong merge request author' unless merge_request.author == author
raise 'task user is not the assignee' unless merge_request.assignees.include?(task_user)
raise 'task discussion was not reset' unless merge_request.notes.where(system: false).count.zero?
distractor_merge_requests.each do |distractor|
  raise "distractor !#{distractor.iid} is not open" unless distractor.open?
  unless distractor.assignees.include?(task_user)
    raise "distractor !#{distractor.iid} is not assigned to the task user"
  end
end

puts [
  phase == 'settle' ? 'TASK_MR_SETTLED' : 'TASK_MR_READY',
  "project=#{project.full_path}",
  "iid=#{merge_request.iid}",
  "state=#{merge_request.state}",
  "author=#{merge_request.author.username}",
  "assignee=#{merge_request.assignees.first.username}",
  "pipeline=#{pipeline.status}",
  "distractors=#{distractor_merge_requests.map(&:iid).sort.join(',')}",
  "distractor_pipelines=#{distractor_pipelines.map(&:status).uniq.join(',')}",
  "removed_notes=#{removed_notes}"
].join(' ')
