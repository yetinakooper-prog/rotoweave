using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Text;
using UnityEditor;
using UnityEditor.Animations;
using UnityEngine;

namespace RotoWeave.Editor
{
    internal static class RotoWeaveCharacterEditableExtractor
    {
        public static void CreateEditableCopy(RotoWeaveCharacterImporter importer)
        {
            if (importer == null) throw new ArgumentNullException(nameof(importer));
            var sourceAssetPath = importer.assetPath;
            var manifest = ReadManifest(sourceAssetPath);
            var sourcePrefab = AssetDatabase.LoadAssetAtPath<GameObject>(sourceAssetPath);
            var sourceController = AssetDatabase.LoadAllAssetsAtPath(sourceAssetPath)
                .OfType<AnimatorController>()
                .SingleOrDefault();
            if (sourcePrefab == null || sourceController == null)
            {
                throw new InvalidOperationException("源角色尚未完整导入，请先点击 Apply 或重新导入。");
            }

            var safeCharacterName = SafeName(
                string.IsNullOrWhiteSpace(manifest.character.name)
                    ? sourcePrefab.name
                    : manifest.character.name,
                "RotoWeaveCharacter");
            var folderName = safeCharacterName + "_Editable_v" +
                             manifest.character.revision.ToString("D4");
            var sourceDirectory = Path.GetDirectoryName(sourceAssetPath)
                ?.Replace('\\', '/') ?? "Assets";
            var sourceDirectoryAbsolute = AssetPathToAbsolute(sourceDirectory);
            var selectedParent = EditorUtility.SaveFolderPanel(
                "选择可编辑副本的父目录",
                sourceDirectoryAbsolute,
                string.Empty);
            if (string.IsNullOrEmpty(selectedParent)) return;

            var parentAssetPath = AbsoluteToAssetPath(selectedParent);
            var targetFolder = parentAssetPath.TrimEnd('/') + "/" + folderName;
            EnsureEmptyOrMissing(targetFolder);
            Extract(manifest, sourcePrefab, sourceController, targetFolder);
        }

        private static void Extract(
            RotoWeaveCharacterManifest manifest,
            GameObject sourcePrefab,
            AnimatorController sourceController,
            string targetFolder)
        {
            var targetAbsolute = AssetPathToAbsolute(targetFolder);
            var directoryExisted = Directory.Exists(targetAbsolute);
            var createdAssets = new List<string>();
            try
            {
                if (!directoryExisted)
                {
                    Directory.CreateDirectory(targetAbsolute);
                }
                AssetDatabase.Refresh();

                var sourceLayer = sourceController.layers.FirstOrDefault();
                if (sourceLayer == null || sourceLayer.stateMachine == null)
                {
                    throw new InvalidOperationException("源 AnimatorController 缺少 Base Layer。");
                }
                var sourceStates = sourceLayer.stateMachine.states
                    .ToDictionary(item => item.state.name, StringComparer.Ordinal);

                var editableClips = new Dictionary<string, AnimationClip>(StringComparer.Ordinal);
                for (var index = 0; index < manifest.animations.Length; index++)
                {
                    var animation = manifest.animations[index];
                    if (!sourceStates.TryGetValue(animation.displayName, out var sourceStateData) ||
                        !(sourceStateData.state.motion is AnimationClip sourceClip))
                    {
                        throw new InvalidOperationException(
                            "源控制器缺少动画状态：" + animation.displayName + "。");
                    }

                    var clipCopy = UnityEngine.Object.Instantiate(sourceClip);
                    clipCopy.name = animation.displayName;
                    clipCopy.hideFlags = HideFlags.None;
                    var clipPath = UniqueAssetPath(
                        targetFolder,
                        index.ToString("D2") + "_" + SafeName(animation.displayName, "Animation"),
                        ".anim");
                    createdAssets.Add(clipPath);
                    AssetDatabase.CreateAsset(clipCopy, clipPath);
                    editableClips.Add(animation.id, clipCopy);
                }

                var controllerPath = targetFolder + "/" +
                                     SafeName(manifest.character.name, "RotoWeaveCharacter") +
                                     "_Editable.controller";
                createdAssets.Add(controllerPath);
                var editableController =
                    AnimatorController.CreateAnimatorControllerAtPath(controllerPath);
                editableController.name =
                    SafeName(manifest.character.name, "RotoWeaveCharacter") +
                    "_Editable";
                foreach (var parameter in sourceController.parameters)
                {
                    editableController.AddParameter(new AnimatorControllerParameter
                    {
                        name = parameter.name,
                        type = parameter.type,
                        defaultBool = parameter.defaultBool,
                        defaultFloat = parameter.defaultFloat,
                        defaultInt = parameter.defaultInt
                    });
                }

                var editableLayers = editableController.layers;
                var editableLayer = editableLayers[0];
                editableLayer.name = sourceLayer.name;
                editableLayer.defaultWeight = sourceLayer.defaultWeight;
                editableLayer.avatarMask = sourceLayer.avatarMask;
                editableLayer.blendingMode = sourceLayer.blendingMode;
                editableLayer.iKPass = sourceLayer.iKPass;
                editableLayer.syncedLayerAffectsTiming =
                    sourceLayer.syncedLayerAffectsTiming;
                editableController.layers = editableLayers;
                var editableStateMachine = editableController.layers[0].stateMachine;
                editableStateMachine.name = sourceLayer.stateMachine.name;

                AnimatorState editableDefault = null;
                foreach (var animation in manifest.animations)
                {
                    var sourceStateData = sourceStates[animation.displayName];
                    var sourceState = sourceStateData.state;
                    var editableState = editableStateMachine.AddState(
                        animation.displayName,
                        sourceStateData.position);
                    editableState.motion = editableClips[animation.id];
                    editableState.speed = sourceState.speed;
                    editableState.cycleOffset = sourceState.cycleOffset;
                    editableState.mirror = sourceState.mirror;
                    editableState.iKOnFeet = sourceState.iKOnFeet;
                    editableState.writeDefaultValues = sourceState.writeDefaultValues;
                    editableState.tag = sourceState.tag;
                    if (animation.id == manifest.character.defaultAnimationId)
                    {
                        editableDefault = editableState;
                    }
                }
                if (editableDefault == null)
                {
                    throw new InvalidOperationException("默认动画不在角色清单中。");
                }
                editableStateMachine.defaultState = editableDefault;
                EditorUtility.SetDirty(editableController);
                EditorUtility.SetDirty(editableStateMachine);
                AssetDatabase.SaveAssets();

                var sourcePrefabType = PrefabUtility.GetPrefabAssetType(sourcePrefab);
                if (sourcePrefabType == PrefabAssetType.NotAPrefab)
                {
                    throw new InvalidOperationException(
                        "导入角色不是可继承的 Prefab 资产，无法创建 Prefab Variant。");
                }

                var instance = PrefabUtility.InstantiatePrefab(sourcePrefab) as GameObject;
                if (instance == null)
                {
                    throw new InvalidOperationException("无法实例化源角色 Prefab。");
                }
                var prefabPath = targetFolder + "/" +
                                 SafeName(manifest.character.name, "RotoWeaveCharacter") +
                                 "_Editable.prefab";
                createdAssets.Add(prefabPath);
                try
                {
                    instance.name =
                        SafeName(manifest.character.name, "RotoWeaveCharacter") +
                        "_Editable";
                    var animator = instance.GetComponent<Animator>();
                    if (animator == null)
                    {
                        throw new InvalidOperationException("源角色 Prefab 缺少 Animator。");
                    }
                    animator.runtimeAnimatorController = editableController;
                    PrefabUtility.RecordPrefabInstancePropertyModifications(animator);
                    PrefabUtility.SaveAsPrefabAsset(instance, prefabPath, out var saved);
                    if (!saved)
                    {
                        throw new InvalidOperationException("Prefab Variant 保存失败。");
                    }
                }
                finally
                {
                    UnityEngine.Object.DestroyImmediate(instance);
                }

                AssetDatabase.SaveAssets();
                AssetDatabase.Refresh();
                var editablePrefab = AssetDatabase.LoadAssetAtPath<GameObject>(prefabPath);
                if (editablePrefab == null ||
                    PrefabUtility.GetPrefabAssetType(editablePrefab) !=
                    PrefabAssetType.Variant)
                {
                    throw new InvalidOperationException(
                        "生成结果不是 Prefab Variant，已取消提取。");
                }
                var editableAnimator = editablePrefab.GetComponent<Animator>();
                if (editableAnimator == null ||
                    editableAnimator.runtimeAnimatorController != editableController)
                {
                    throw new InvalidOperationException(
                        "Prefab Variant 未正确引用可写 AnimatorController，已取消提取。");
                }

                Selection.activeObject = editablePrefab;
                EditorGUIUtility.PingObject(editablePrefab);
                EditorUtility.DisplayDialog(
                    "可编辑副本已创建",
                    "已创建独立 AnimationClip、AnimatorController 和 Prefab Variant。\n\n" +
                    "Clip 继续引用源 .rotoweave 中的共享 Sprite；重新导入源包不会覆盖本目录。",
                    "确定");
            }
            catch
            {
                for (var index = createdAssets.Count - 1; index >= 0; index--)
                {
                    AssetDatabase.DeleteAsset(createdAssets[index]);
                }
                if (!directoryExisted && AssetDatabase.IsValidFolder(targetFolder))
                {
                    AssetDatabase.DeleteAsset(targetFolder);
                }
                AssetDatabase.Refresh();
                throw;
            }
        }

        private static RotoWeaveCharacterManifest ReadManifest(string sourceAssetPath)
        {
            using (var stream = File.OpenRead(sourceAssetPath))
            using (var archive = new ZipArchive(stream, ZipArchiveMode.Read, false))
            {
                var entry = archive.GetEntry("manifest.json");
                if (entry == null) throw new InvalidDataException("源包缺少 manifest.json。");
                using (var reader = new StreamReader(
                           entry.Open(),
                           new UTF8Encoding(false, true),
                           true))
                {
                    var manifest =
                        JsonUtility.FromJson<RotoWeaveCharacterManifest>(reader.ReadToEnd());
                    if (manifest == null || manifest.character == null ||
                        manifest.animations == null ||
                        manifest.animations.Length == 0)
                    {
                        throw new InvalidDataException("源包角色清单无效。");
                    }
                    return manifest;
                }
            }
        }

        private static string UniqueAssetPath(
            string folder,
            string baseName,
            string extension)
        {
            var candidate = folder + "/" + baseName + extension;
            if (AssetDatabase.LoadMainAssetAtPath(candidate) == null &&
                !File.Exists(AssetPathToAbsolute(candidate)))
            {
                return candidate;
            }
            throw new IOException("目标目录包含同名资源：" + candidate + "。");
        }

        private static void EnsureEmptyOrMissing(string targetFolder)
        {
            var targetAbsolute = AssetPathToAbsolute(targetFolder);
            if (!Directory.Exists(targetAbsolute)) return;
            if (Directory.EnumerateFileSystemEntries(targetAbsolute).Any())
            {
                throw new IOException(
                    "目标目录已存在且不为空，绝不会覆盖或合并已有文件：\n" +
                    targetFolder);
            }
        }

        private static string AbsoluteToAssetPath(string absolutePath)
        {
            var normalized = Path.GetFullPath(absolutePath)
                .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            var assetsRoot = Path.GetFullPath(Application.dataPath)
                .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            if (!normalized.Equals(assetsRoot, StringComparison.OrdinalIgnoreCase) &&
                !normalized.StartsWith(
                    assetsRoot + Path.DirectorySeparatorChar,
                    StringComparison.OrdinalIgnoreCase))
            {
                throw new IOException("可编辑副本必须创建在当前 Unity 项目的 Assets 目录内。");
            }
            return "Assets" + normalized.Substring(assetsRoot.Length)
                .Replace('\\', '/');
        }

        private static string AssetPathToAbsolute(string assetPath)
        {
            var projectRoot = Path.GetFullPath(
                Path.Combine(Application.dataPath, ".."));
            return Path.GetFullPath(
                Path.Combine(projectRoot, assetPath.Replace('/', Path.DirectorySeparatorChar)));
        }

        private static string SafeName(string value, string fallback)
        {
            var invalid = new HashSet<char>(Path.GetInvalidFileNameChars());
            invalid.Add('/');
            invalid.Add('\\');
            var result = new string((value ?? string.Empty)
                .Trim()
                .Select(character => invalid.Contains(character) ? '_' : character)
                .ToArray())
                .Trim('.', ' ');
            return string.IsNullOrEmpty(result) ? fallback : result;
        }
    }
}
