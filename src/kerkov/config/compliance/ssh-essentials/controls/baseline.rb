{
  'ssh-no-empty-passwords' => ['permitemptypasswords', 'no'],
  'ssh-ignore-rhosts' => ['ignorerhosts', 'yes'],
  'ssh-no-x11' => ['x11forwarding', 'no'],
  'ssh-auth-retries' => ['maxauthtries', '4'],
  'ssh-login-grace' => ['logingracetime', '60']
}.each do |id, (setting, value)|
  control id do
    impact 0.6
    title "Effective SSH configuration sets #{setting} to #{value}"
    describe command('/usr/sbin/sshd -T') do
      its('exit_status') { should eq 0 }
      its('stdout') { should match(/^#{setting} #{value}$/) }
    end
  end
end
