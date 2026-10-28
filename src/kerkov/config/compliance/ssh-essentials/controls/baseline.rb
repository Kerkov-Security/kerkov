{
  'ssh-no-empty-passwords' => ['permitemptypasswords', 'no'],
  'ssh-ignore-rhosts' => ['ignorerhosts', 'yes'],
  'ssh-no-x11' => ['x11forwarding', 'no'],
  'ssh-auth-retries' => ['maxauthtries', '[1-4]'],
  'ssh-login-grace' => ['logingracetime', '(?:[1-9]|[1-5][0-9]|60)']
}.each do |id, (setting, value)|
  control id do
    impact 0.6
    title "Effective SSH configuration satisfies the #{setting} baseline"
    describe command('/usr/sbin/sshd -T') do
      its('exit_status') { should eq 0 }
      its('stdout') { should match(/^#{setting} #{value}$/) }
    end
  end
end
