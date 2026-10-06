# Seed records for GitLab 15.11. The dotfiles repository itself is pushed from
# the real upstream Git bundle after this metadata is created.
settings = ApplicationSetting.current
settings.update!(default_branch_protection: 0) unless settings.default_branch_protection == 0

user = User.find_or_initialize_by(username: 'byteblaze')
user.name = 'Byte Blaze'
user.email = 'byteblaze@example.test'
user.password = 'V9xQ4mL2pR7s!'
user.password_confirmation = 'V9xQ4mL2pR7s!'
user.admin = false
user.skip_confirmation! if user.respond_to?(:skip_confirmation!)
user.save!
Users::Callout.find_or_create_by!(user_id: user.id, feature_name: 'vscode_web_ide_callout')

# The recording shows 24 projects available to Byte Blaze. Its first page mixes
# personal projects with three real public-project namespaces. The additional
# personal entries below are real repositories from the upstream owner's
# public inventory; only dotfiles needs repository contents for this task.
groups = {
  a11y: Group.find_or_initialize_by(path: 'the-a11y-project'),
  nlab: Group.find_or_initialize_by(path: 'n-lab'),
  primer: Group.find_or_initialize_by(path: 'primer')
}
groups[:a11y].name = 'The A11Y Project'
groups[:nlab].name = 'n-lab'
groups[:primer].name = 'Primer'
groups.each_value do |group|
  group.visibility_level = Gitlab::VisibilityLevel::PUBLIC
  group.save!
end
groups[:a11y].add_maintainer(user) unless groups[:a11y].members.include?(user)
groups[:nlab].add_owner(user) unless groups[:nlab].members.include?(user)
groups[:primer].add_developer(user) unless groups[:primer].members.include?(user)

projects = [
  [:personal, '2019-nCov', '2019-ncov', 'Use Google Maps Timeline data to compare with COVID-19 patient history location.', :owner],
  [:personal, 'a11y-syntax-highlighting', 'a11y-syntax-highlighting', '💄 Accessible light and dark syntax highlighting themes', :owner],
  [:personal, 'a11y-webring.club', 'a11y-webring-club', '🌐 A webring for digital accessibility practitioners.', :owner],
  [:a11y, 'a11yproject.com', 'a11yproject-com', 'The A11Y Project is a community-driven effort to make digital accessibility easier.', :maintainer],
  [:personal, 'accessible-html-content-patterns', 'accessible-html-content-patterns', '♿️ The full HTML5 Doctor Element Index as well as common markup patterns for quick reference.', :owner],
  [:nlab, 'AutoAGI', 'autoagi', '', :owner],
  [:nlab, 'awesome-llms', 'awesome-llms', '', :owner],
  [:personal, 'Chatgpt', 'chatgpt', 'Experiments and notes for conversational AI tools.', :owner],
  [:personal, 'cloud-to-butt', 'cloud-to-butt', "Chrome extension that replaces occurrences of 'the cloud' with 'my butt'", :owner],
  [:primer, 'design', 'design', 'Primer Design Guidelines', :developer],
  [:personal, 'dotfiles', 'dotfiles', '🤖 Computer setup', :owner],
  [:personal, 'emily-and-eric.wedding', 'emily-and-eric-wedding', '💍 October 12, 2019', :owner],
  [:personal, 'emoji', 'emoji', "🎉 I'm sorry.", :owner],
  [:personal, 'empathy-prompts', 'empathy-prompts', '💡 Ideas to help consider Inclusive Design principles when making things for others to use.', :owner],
  [:personal, 'enchilada', 'enchilada', '🌯 A collection of files and opinionated code used to set up projects.', :owner],
  [:personal, 'ericwbailey', 'ericwbailey', 'README', :owner],
  [:personal, 'ericwbailey.website', 'ericwbailey-website', '📐 Repo for my personal website.', :owner],
  [:personal, 'favicon', 'favicon', '🖼 An attempt to capture all possible favicons for a web project.', :owner],
  [:personal, 'gimmiethat.space', 'gimmiethat-space', 'I need some space.', :owner],
  [:personal, 'inclusive-components', 'inclusive-components', 'A blog trying to be a pattern library. All about designing inclusive web interfaces.', :owner],
  [:personal, 'project-guidelines', 'project-guidelines', 'A set of best practices for JavaScript projects', :owner],
  [:personal, 'stylelint-a11y', 'stylelint-a11y', 'A collection of accessibility rules for stylelint', :owner],
  [:personal, 'tab-closer', 'tab-closer', 'Closes browser tabs that automatically open after launching a service', :owner],
  [:personal, 'unreliable-narrator', 'unreliable-narrator', 'A potentially treacherous public domain font.', :owner]
]

seeded_projects = projects.map do |namespace_key, name, path, description, role|
  namespace = namespace_key == :personal ? user.namespace : groups.fetch(namespace_key)
  project = Project.find_or_initialize_by(namespace_id: namespace.id, path: path)
  project.name = name
  project.description = description
  project.visibility_level = Gitlab::VisibilityLevel::PUBLIC
  project.creator = user if project.respond_to?(:creator=)
  project.save!
  unless project.team.member?(user)
    case role
    when :developer then project.add_developer(user)
    when :maintainer then project.add_maintainer(user)
    else project.add_owner(user)
    end
  end
  if path == 'dotfiles' && !project.repository_exists?
    project.create_repository
  end
  project
end

seeded_projects.first(3).each do |project|
  user.toggle_star(project) unless user.starred?(project)
end

puts "SEED_OK user=#{user.username} projects=#{seeded_projects.count} starred=#{user.starred_projects.count}"
