# frozen_string_literal: true

# Reconstruct the dependency-complete WebArena GitLab baseline using semantic
# identifiers. This script runs inside the Omnibus Rails environment:
#
#   gitlab-rails runner seed_gitlab.rb DATASET_DIR OUTPUT_DIR

require 'digest'
require 'fileutils'
require 'json'
require 'time'

DATASET_DIR = File.expand_path(ARGV.fetch(0))
OUTPUT_DIR = File.expand_path(ARGV.fetch(1))
SEED_PASSWORD = ENV.fetch('GITLAB_SEED_USER_PASSWORD', 'N7v!4Qz@8Lm#2Rx%')

class SeedError < StandardError; end

def document(name)
  JSON.parse(File.read(File.join(DATASET_DIR, name)))
end

def records(name)
  document(name).fetch('records')
end

def parse_time(value)
  value.nil? || value.to_s.empty? ? nil : Time.zone.parse(value.to_s)
end

def assign_supported(record, attributes)
  attributes.each do |key, value|
    record.public_send("#{key}=", value) if record.has_attribute?(key)
  end
end

def update_supported(record, attributes)
  filtered = attributes.select { |key, _value| record.has_attribute?(key) }
  record.update_columns(filtered) unless filtered.empty?
end

def unwrap_service(result, key)
  return result if result.is_a?(ApplicationRecord)

  payload = result.respond_to?(:payload) ? result.payload : nil
  value = payload && (payload[key] || payload[key.to_s])
  return value if value

  message = if result.respond_to?(:message)
              result.message
            elsif result.respond_to?(:errors)
              result.errors.full_messages.join(', ')
            else
              result.inspect
            end
  raise SeedError, "GitLab service did not return #{key}: #{message}"
end

def user!(username)
  User.find_by_username(username) || raise(SeedError, "missing user #{username}")
end

def project!(path)
  Project.find_by_full_path(path) || raise(SeedError, "missing project #{path}")
end

def project_label!(project, title)
  ProjectLabel.find_by(project_id: project.id, title: title) ||
    raise(SeedError, "missing label #{project.full_path}::#{title}")
end

def issue!(semantic_key)
  path, iid = semantic_key.split('#', 2)
  project!(path).issues.find_by_iid(iid.to_i) || raise(SeedError, "missing issue #{semantic_key}")
end

def merge_request!(semantic_key)
  path, iid = semantic_key.split('!', 2)
  project!(path).merge_requests.find_by_iid(iid.to_i) ||
    raise(SeedError, "missing merge request #{semantic_key}")
end

def create_user!(record)
  account = record.fetch('account')
  username = record.fetch('canonical_username')
  user = User.find_by_username(username)

  if user.nil?
    admin = User.find_by_username('root') || raise(SeedError, 'GitLab root account is missing')
    params = {
      username: username,
      name: account['name'].presence || username,
      email: account['email'].presence || "#{username.downcase.gsub(/[^a-z0-9_.-]/, '_')}@seed.invalid",
      password: SEED_PASSWORD,
      password_confirmation: SEED_PASSWORD,
      skip_confirmation: true,
      organization_id: Organizations::Organization.default_organization.id
    }
    user = unwrap_service(
      Users::CreateService.new(admin, params).execute,
      :user
    )
    unless user.persisted?
      raise SeedError, "failed to create user #{username}: #{user.errors.full_messages.join(', ')}"
    end
  end

  unless username == 'ghost'
    assign_supported(
      user,
      name: account['name'].presence || username,
      email: account['email'].presence || user.email,
      admin: account['admin'] || username == 'root',
      auditor: account['auditor'] || false,
      can_create_group: account['can_create_group'],
      external: account['external'],
      location: account['location'],
      organization: account['organization'],
      preferred_language: account['preferred_language'],
      private_profile: account['private_profile'],
      projects_limit: account['projects_limit'],
      public_email: account['public_email'],
      website_url: account['website_url']
    )
    user.password = SEED_PASSWORD
    user.password_confirmation = SEED_PASSWORD
    user.skip_confirmation! if user.respond_to?(:skip_confirmation!)
    user.save!
    update_supported(
      user,
      created_at: parse_time(account['created_at']),
      updated_at: parse_time(account['updated_at']),
      feed_token: account['feed_token'],
      commit_email: account['commit_email']
    )
  end

  raise SeedError, "user namespace was not created for #{username}" unless user.namespace

  user
end

def ensure_namespace_owner!(record)
  username = record.fetch('owner_username')
  existing = User.find_by_username(username)
  return [existing, false] if existing

  synthetic = {
    'canonical_username' => username,
    'account' => {
      'name' => record['name'].presence || username,
      'email' => "#{username.downcase.gsub(/[^a-z0-9_.-]/, '_')}@namespace.seed.invalid",
      'admin' => false,
      'auditor' => false,
      'can_create_group' => false,
      'external' => false,
      'private_profile' => false,
      'projects_limit' => 100_000
    }
  }
  [create_user!(synthetic), true]
end

def create_project!(record, root)
  namespace_owner = user!(record.fetch('namespace_owner_username'))
  namespace = namespace_owner.namespace
  params = {
    name: record.fetch('name'),
    path: record.fetch('path'),
    namespace_id: namespace.id,
    description: record['description'],
    visibility_level: record.fetch('visibility_level')
  }
  project = unwrap_service(Projects::CreateService.new(root, params).execute, :project)
  unless project.persisted?
    raise SeedError, "failed to create #{record['path_with_namespace']}: #{project.errors.full_messages.join(', ')}"
  end
  unless project.full_path.casecmp(record.fetch('path_with_namespace')).zero?
    raise SeedError, "created project at #{project.full_path}, expected #{record['path_with_namespace']}"
  end

  update_supported(
    project,
    archived: record['archived'],
    builds_access_level: record['builds_access_level'],
    created_at: parse_time(record['created_at']),
    default_branch: record['default_branch'],
    description: record['description'],
    forking_access_level: record['forking_access_level'],
    issues_access_level: record['issues_access_level'],
    issues_template: record['issues_template'],
    last_activity_at: parse_time(record['last_activity_at']),
    lfs_enabled: record['lfs_enabled'],
    merge_requests_access_level: record['merge_requests_access_level'],
    merge_requests_ff_only_enabled: record['merge_requests_ff_only_enabled'],
    merge_requests_rebase_enabled: record['merge_requests_rebase_enabled'],
    merge_requests_template: record['merge_requests_template'],
    only_allow_merge_if_all_discussions_are_resolved: record['only_allow_merge_if_all_discussions_are_resolved'],
    only_allow_merge_if_pipeline_succeeds: record['only_allow_merge_if_pipeline_succeeds'],
    pages_access_level: record['pages_access_level'],
    remove_source_branch_after_merge: record['remove_source_branch_after_merge'],
    repository_access_level: record['repository_access_level'],
    request_access_enabled: record['request_access_enabled'],
    shared_runners_enabled: record['shared_runners_enabled'],
    snippets_access_level: record['snippets_access_level'],
    star_count: record['star_count'],
    updated_at: parse_time(record['updated_at']),
    visibility_level: record['visibility_level'],
    wiki_access_level: record['wiki_access_level']
  )
  project.reset
end

def restore_repository!(record)
  project = project!(record.fetch('project'))
  bundle = File.join(DATASET_DIR, record.fetch('artifact'))
  raise SeedError, "missing bundle #{bundle}" unless File.file?(bundle)

  actual_sha = Digest::SHA256.file(bundle).hexdigest
  unless actual_sha == record.fetch('sha256')
    raise SeedError, "bundle checksum mismatch for #{project.full_path}: #{actual_sha}"
  end

  shared_class = Struct.new(:export_path, :logger) do
    def error(message)
      raise SeedError, message
    end
  end
  shared = shared_class.new(File.dirname(bundle), Rails.logger)

  RequestStore.begin!
  restored = Gitlab::ImportExport::RepoRestorer.new(
    path_to_bundle: bundle,
    shared: shared,
    importable: project
  ).restore
  raise SeedError, "repository restorer returned false for #{project.full_path}" if restored == false
ensure
  RequestStore.end! if RequestStore.active?
  RequestStore.clear!
end

def create_member!(record, root)
  project = project!(record.fetch('project'))
  user = user!(record.fetch('username'))
  member = project.project_members.find_or_initialize_by(user_id: user.id)
  member.access_level = record.fetch('access_level')
  member.created_by = User.find_by_username(record['created_by_username']) || root
  member.expires_at = record['expires_at']
  member.member_namespace_id = project.project_namespace_id if member.has_attribute?(:member_namespace_id)
  member.save!(validate: false)
  update_supported(member, created_at: parse_time(record['created_at']))

  return unless defined?(NotificationSetting) && record['notification_level']

  setting = NotificationSetting.find_or_initialize_by(user: user, source: project)
  setting.level = record['notification_level']
  setting.save!
end

def create_label!(record)
  project = project!(record.fetch('project'))
  label = ProjectLabel.find_or_initialize_by(project_id: project.id, title: record.fetch('name'))
  assign_supported(
    label,
    color: record.fetch('color'),
    description: record['description'],
    template: record['template']
  )
  label.save!
  update_supported(
    label,
    created_at: parse_time(record['created_at']),
    updated_at: parse_time(record['updated_at'])
  )
end

def create_milestone!(record)
  project = project!(record.fetch('project'))
  milestone = Milestone.new(
    project: project,
    iid: record.fetch('iid'),
    title: record.fetch('title'),
    description: record['description'],
    start_date: record['start_date'],
    due_date: record['due_date']
  )
  milestone.save!
  update_supported(
    milestone,
    created_at: parse_time(record['created_at']),
    updated_at: parse_time(record['updated_at']),
    state: record['state']
  )
  unless milestone.iid == record.fetch('iid')
    raise SeedError, "milestone IID mismatch for #{record['semantic_key']}"
  end
end

def create_issue!(record, root)
  project = project!(record.fetch('project'))
  params = {
    title: record.fetch('title'),
    description: record['description'],
    iid: record.fetch('iid'),
    confidential: record['confidential'],
    created_at: parse_time(record['created_at'])
  }
  issue = unwrap_service(
    Issues::CreateService.new(container: project, current_user: root, params: params).execute,
    :issue
  )
  unless issue.persisted?
    raise SeedError, "failed to create #{record['semantic_key']}: #{issue.errors.full_messages.join(', ')}"
  end
  unless issue.iid == record.fetch('iid')
    raise SeedError, "GitLab #{Gitlab::VERSION} ignored requested issue IID #{record['iid']} for #{project.full_path}"
  end

  issue.issue_assignees.delete_all
  Array(record['assignees']).each do |username|
    IssueAssignee.create!(issue: issue, assignee: user!(username))
  end
  issue.label_links.delete_all
  Array(record['labels']).each do |title|
    LabelLink.create!(target: issue, label: project_label!(project, title))
  end
  milestone = record['milestone_title'] && project.milestones.find_by(title: record['milestone_title'])
  state_id = Issue.available_states.fetch(record.fetch('state'))
  update_supported(
    issue,
    description: record['description'],
    author_id: user!(record.fetch('author_username')).id,
    closed_at: parse_time(record['closed_at']),
    closed_by_id: record['closed_by_username'] && user!(record['closed_by_username']).id,
    confidential: record['confidential'],
    discussion_locked: record['discussion_locked'],
    due_date: record['due_date'],
    milestone_id: milestone&.id,
    state_id: state_id,
    updated_at: parse_time(record['updated_at']),
    updated_by_id: record['updated_by_username'] && user!(record['updated_by_username']).id,
    weight: record['weight']
  )
end

def create_merge_request!(record, root)
  target = project!(record.fetch('project'))
  source = project!(record.fetch('source_project'))
  params = {
    iid: record.fetch('iid'),
    source_branch: record.fetch('source_branch'),
    target_branch: record.fetch('target_branch'),
    source_project_id: source.id,
    target_project_id: target.id,
    title: record.fetch('title'),
    description: record['description'],
    allow_collaboration: record['allow_maintainer_to_push'],
    squash: record['squash']
  }
  merge_request = unwrap_service(
    MergeRequests::CreateService.new(project: target, current_user: root, params: params).execute,
    :merge_request
  )
  unless merge_request.persisted?
    raise SeedError,
      "failed to create #{record['semantic_key']}: #{merge_request.errors.full_messages.join(', ')}"
  end
  unless merge_request.iid == record.fetch('iid')
    raise SeedError,
      "GitLab #{Gitlab::VERSION} ignored requested merge request IID #{record['iid']} for #{target.full_path}"
  end

  merge_request.merge_request_assignees.delete_all
  Array(record['assignees']).each do |username|
    MergeRequestAssignee.create!(merge_request: merge_request, assignee: user!(username))
  end
  merge_request.merge_request_reviewers.delete_all
  Array(record['reviewers']).each do |username|
    MergeRequestReviewer.create!(merge_request: merge_request, reviewer: user!(username))
  end
  merge_request.label_links.delete_all
  Array(record['labels']).each do |title|
    LabelLink.create!(target: merge_request, label: project_label!(target, title))
  end
  milestone = record['milestone_title'] && target.milestones.find_by(title: record['milestone_title'])
  state_id = MergeRequest.available_states.fetch(record.fetch('state'))
  update_supported(
    merge_request,
    description: record['description'],
    allow_maintainer_to_push: record['allow_maintainer_to_push'],
    author_id: user!(record.fetch('author_username')).id,
    created_at: parse_time(record['created_at']),
    discussion_locked: record['discussion_locked'],
    draft: record['draft'],
    merge_commit_sha: record['merge_commit_sha'],
    merge_user_id: record['merge_user_username'] && user!(record['merge_user_username']).id,
    milestone_id: milestone&.id,
    squash: record['squash'],
    state_id: state_id,
    updated_at: parse_time(record['updated_at'])
  )
end

def create_note!(record)
  noteable = record.fetch('parent_type') == 'issue' ? issue!(record.fetch('parent')) :
    merge_request!(record.fetch('parent'))
  note = Note.new(
    project: project!(record.fetch('project')),
    noteable: noteable,
    author: user!(record.fetch('author_username')),
    note: record.fetch('body'),
    created_at: parse_time(record['created_at']),
    updated_at: parse_time(record['updated_at'])
  )
  assign_supported(
    note,
    confidential: record['confidential'],
    discussion_id: record['discussion_id'],
    internal: record['internal'],
    system: record['system']
  )
  # The extraction intentionally contains no diff-position payload. Preserve
  # LegacyDiffNote bodies as ordinary notes instead of creating broken STI
  # records that the GitLab UI cannot render.
  note.save!(validate: false)
end

manifest = document('manifest.json')
project_records = records('projects.json')
exclusions = document('excluded_initial_entities.json').fetch('categories')
root = User.find_by_username('root') || raise(SeedError, 'GitLab root account is missing')

puts "[seed] GitLab #{Gitlab::VERSION}; dataset #{manifest['dataset']}"

excluded_project_paths = %w[task_created_projects task_created_fork_projects].flat_map do |category|
  Array(exclusions[category]).map { |item| item.fetch('path_with_namespace') }
end
(project_records.map { |item| item.fetch('path_with_namespace') } + excluded_project_paths).uniq.reverse_each do |path|
  existing = Project.find_by_full_path(path)
  next unless existing

  puts "[seed] removing existing managed project #{path}"
  existing.destroy!
end
Array(exclusions['task_created_groups']).each do |item|
  existing = Group.find_by_full_path(item.fetch('full_path'))
  existing&.destroy!
end

account_records = records('users.json').select { |item| item['identity_type'] == 'gitlab_account' }
account_records.group_by { |item| item.fetch('canonical_username').downcase }.each_value do |duplicates|
  create_user!(duplicates.first)
end
root = user!('root')
puts "[seed] users: #{account_records.map { |item| item['canonical_username'].downcase }.uniq.length} accounts"

namespace_records = records('groups.json')
structural_namespace_accounts = namespace_records.filter_map do |item|
  next unless item.fetch('namespace_type') == 'User'

  ensure_namespace_owner!(item)
  username = item.fetch('owner_username')
  account_records.any? { |account| account.fetch('canonical_username').casecmp(username).zero? } ? nil : username
end
puts "[seed] structural namespace accounts: #{structural_namespace_accounts.length}"

project_records.each { |item| create_project!(item, root) }
puts "[seed] projects: #{project_records.length}"

records('project_members.json').each { |item| create_member!(item, root) }
puts "[seed] project memberships: #{records('project_members.json').length}"

repository_records = records('repository_metadata.json')
repository_records.each do |item|
  puts "[seed] restoring #{item['project']} (#{item['size_bytes']} bytes, #{item['ref_count']} refs)"
  restore_repository!(item)
  project = project!(item.fetch('project'))
  update_supported(project, default_branch: item.fetch('default_branch'))
end
puts "[seed] repositories: #{repository_records.length}"

# Project creation can install instance defaults. The extracted label set is
# authoritative, so remove project labels before recreating exactly that set.
ProjectLabel.where(project_id: project_records.map { |item| project!(item.fetch('path_with_namespace')).id }).delete_all
records('labels.json').each { |item| create_label!(item) }
records('milestones.json').each { |item| create_milestone!(item) }
records('issues.json').sort_by { |item| [item['project'], item['iid']] }.each { |item| create_issue!(item, root) }
records('merge_requests.json').sort_by { |item| [item['project'], item['iid']] }.each do |item|
  create_merge_request!(item, root)
end
records('notes.json').sort_by { |item| [item['project'], item['parent'], item['sequence_index']] }.each do |item|
  create_note!(item)
end
# Creating native merge requests can recompute GitLab-managed merge refs.
# Reapply only MR-touched bundles so the final ref maps remain canonical.
merge_request_repository_paths = records('merge_requests.json').flat_map do |item|
  [item.fetch('project'), item.fetch('source_project')]
end.uniq
repository_records.each do |item|
  next unless merge_request_repository_paths.include?(item.fetch('project'))

  puts "[seed] restoring canonical post-MR refs for #{item['project']}"
  restore_repository!(item)
end


# Restore extracted counters/timestamps after creation callbacks have touched
# them. Star rows are deliberately absent: starring is task-created state.
project_records.each do |item|
  project = project!(item.fetch('path_with_namespace'))
  update_supported(
    project,
    created_at: parse_time(item['created_at']),
    last_activity_at: parse_time(item['last_activity_at']),
    star_count: item['star_count'],
    updated_at: parse_time(item['updated_at'])
  )
end

FileUtils.mkdir_p(OUTPUT_DIR)
summary = {
  'schema_version' => 1,
  'dataset' => manifest.fetch('dataset'),
  'status' => 'seeded',
  'source_manifest_sha256' => Digest::SHA256.file(File.join(DATASET_DIR, 'manifest.json')).hexdigest,
  'semantic_identifier_policy' => manifest.fetch('semantic_identifier_policy'),
  'counts' => {
    'user_or_identity_records' => records('users.json').length,
    'gitlab_accounts' => account_records.map { |item| item['canonical_username'].downcase }.uniq.length,
    'structural_namespace_accounts' => structural_namespace_accounts.length,
    'commit_only_identities' => records('users.json').count { |item| item['identity_type'] == 'git_commit_identity' },
    'namespace_dependencies' => records('groups.json').length,
    'projects' => project_records.length,
    'project_memberships' => records('project_members.json').length,
    'repositories' => repository_records.length,
    'labels' => records('labels.json').length,
    'milestones' => records('milestones.json').length,
    'issues' => records('issues.json').length,
    'merge_requests' => records('merge_requests.json').length,
    'notes' => records('notes.json').length,
    'excluded_initial_entities' => document('excluded_initial_entities.json').fetch('total_count')
  },
  'repository_disk_paths' => repository_records.to_h do |item|
    project = project!(item.fetch('project'))
    [item.fetch('project'), project.repository.disk_path]
  end,
  'normalized_fields' => [
    {
      'field' => 'notes.note_type',
      'source_value' => 'LegacyDiffNote',
      'affected_records' => records('notes.json').count { |item| item['note_type'] == 'LegacyDiffNote' },
      'seeded_value' => nil,
      'reason' => 'the logical extraction has no legacy diff-position payload; ordinary notes preserve body, author, parent, and timestamps without creating unrenderable STI records'
    },
    {
      'field' => 'source_metadata.database_id',
      'seeded_value' => nil,
      'reason' => 'source database IDs are intentionally remapped; all seed resolution uses semantic identifiers'
    },
    {
      'field' => 'projects.repository_storage/storage_version/repository_disk_path',
      'seeded_value' => 'target-instance managed',
      'reason' => 'GitLab assigns storage internals while RepoRestorer imports the canonical bundle'
    }
  ]
}
File.write(File.join(OUTPUT_DIR, 'seed_manifest.json'), JSON.pretty_generate(summary) + "\n")
puts "[seed] labels=#{records('labels.json').length} milestones=#{records('milestones.json').length} " \
     "issues=#{records('issues.json').length} merge_requests=#{records('merge_requests.json').length} " \
     "notes=#{records('notes.json').length}"
puts '[seed] shared baseline construction complete'
