{
  'passwd' => ['root', '0644'],
  'group' => ['root', '0644'],
  'shadow' => ['shadow', '0640'],
  'gshadow' => ['shadow', '0640']
}.each do |name, (group, mode)|
  control "#{name}-protected" do
    impact 0.7
    title "Protect /etc/#{name} ownership and permissions"
    describe file("/etc/#{name}") do
      it { should be_file }
      it { should_not be_symlink }
      its('owner') { should eq 'root' }
      its('group') { should eq group }
      its('mode') { should cmp mode }
    end
  end
end
