control 'passwd-owned-by-root' do
  impact 0.5
  title '/etc/passwd is owned by root'
  desc 'Prevent an ordinary user from owning the system account database.'
  describe file('/etc/passwd') do
    it { should exist }
    its('owner') { should eq 'root' }
  end
end

control 'ssh-root-login-disabled' do
  impact 0.8
  title 'Effective SSH configuration disables direct root login'
  desc 'Verify effective daemon settings, including included configuration files.'
  describe command('/usr/sbin/sshd -T') do
    its('exit_status') { should eq 0 }
    its('stdout') { should match(/^permitrootlogin no$/) }
  end
end
